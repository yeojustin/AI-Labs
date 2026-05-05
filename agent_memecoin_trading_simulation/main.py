#!/usr/bin/env python3
"""Memecoin simulation with LLM-driven agents."""

import asyncio
import json
import logging
import os
import random

import aiohttp

from amm import pool_buy, pool_new, pool_price, pool_sell
from constants import BASE_EVENT_MIX, DEFAULTS, PERSONAS, PERSONA_PROMPTS, SCENARIOS

GEMINI_API_KEY = "AIzaSyD_l8nS05X6y_o5a4YMOYm3Wg6eXO1_5Sw"

def get_env_value(env_key, default_value, value_type="str"):
    raw_value = os.getenv(env_key)
    if raw_value in (None, ""):
        return default_value
    if value_type == "int":
        return int(raw_value)
    if value_type == "float":
        return float(raw_value)
    if value_type == "bool":
        return raw_value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return raw_value


def normalize_weight_map(weight_map):
    total_weight = sum(weight_map.values())
    if total_weight <= 0:
        raise ValueError("weights must sum to > 0")
    return {name: weight / total_weight for name, weight in weight_map.items()}


def parse_persona_weights(persona_weights_text):
    entries = [item.strip() for item in persona_weights_text.split(",") if item.strip()]
    persona_weights = {}
    for entry in entries:
        if "=" not in entry:
            raise ValueError(f"Invalid PERSONAS item: {entry!r}")
        persona_name, raw_weight = entry.split("=", 1)
        persona_name = persona_name.strip()
        if persona_name not in PERSONAS:
            raise ValueError(f"Unknown persona {persona_name!r}; expected one of {PERSONAS}")
        weight = float(raw_weight.strip())
        if weight < 0:
            raise ValueError(f"Negative weight for persona {persona_name!r}")
        persona_weights[persona_name] = weight
    if not persona_weights:
        raise ValueError("No persona weights provided")
    return normalize_weight_map(persona_weights)


def load_config():
    scenario_name = get_env_value("SCENARIO", DEFAULTS["scenario"], "str")
    if scenario_name not in SCENARIOS:
        raise ValueError(f"Unknown SCENARIO={scenario_name!r}. Choose one of: {sorted(SCENARIOS)}")

    persona_override_text = get_env_value("PERSONAS", None, "str")
    if persona_override_text:
        persona_weights = parse_persona_weights(persona_override_text)
    else:
        persona_weights = normalize_weight_map(SCENARIOS[scenario_name]["personas"])

    return {
        "agents": get_env_value("AGENTS", DEFAULTS["agents"], "int"),
        "rounds": get_env_value("ROUNDS", DEFAULTS["rounds"], "int"),
        "seed": get_env_value("SEED", DEFAULTS["seed"], "int"),
        "scenario": scenario_name,
        "initial_agent_usdc": get_env_value("INITIAL_AGENT_USDC", DEFAULTS["initial_agent_usdc"], "float"),
        "pool_usdc": get_env_value("POOL_USDC", DEFAULTS["pool_usdc"], "float"),
        "pool_tokens": get_env_value("POOL_TOKENS", DEFAULTS["pool_tokens"], "float"),
        "fee_bps": get_env_value("FEE_BPS", DEFAULTS["fee_bps"], "float"),
        "personas": persona_weights,
        "save_path": get_env_value("SAVE_PATH", DEFAULTS["save_path"], "str"),
        "save_agents": get_env_value("SAVE_AGENTS", DEFAULTS["save_agents"], "bool"),
        "log_every": get_env_value("LOG_EVERY", DEFAULTS["log_every"], "int"),
        "gemini_model": get_env_value("GEMINI_MODEL", DEFAULTS["gemini_model"], "str"),
        "max_concurrent": get_env_value("MAX_CONCURRENT", DEFAULTS["max_concurrent"], "int"),
        "api_retries": get_env_value("API_RETRIES", DEFAULTS["api_retries"], "int"),
        "api_timeout_s": get_env_value("API_TIMEOUT_S", DEFAULTS["api_timeout_s"], "int"),
    }


def pick_weighted_item(random_generator, weighted_items):
    total_weight = sum(weight for _, weight in weighted_items)
    random_point = random_generator.random() * total_weight
    cumulative_weight = 0.0
    for item_name, item_weight in weighted_items:
        cumulative_weight += item_weight
        if random_point <= cumulative_weight:
            return item_name
    return weighted_items[-1][0]


def sample_market_event(random_generator, event_mix):
    event_type = pick_weighted_item(random_generator, event_mix)
    event_magnitude = float(min(1.0, max(0.0, random_generator.betavariate(1.15, 2.85) * 1.55)))
    if event_type == "SIDEWAYS":
        event_magnitude *= 0.35
    elif event_type in {"CT_X_NEWS_BULL", "CT_X_NEWS_BEAR", "TG_CALLS_PUMP", "TG_PANIC_SELL"}:
        event_magnitude *= 0.8
    return {"type": event_type, "magnitude": event_magnitude}


def build_model_prompt(agent_state, current_price, price_history, current_event, last_round_market):
    recent_prices = price_history[-5:] if len(price_history) >= 5 else price_history
    trend_text = " -> ".join(f"{price:.6f}" for price in recent_prices)

    daily_change_text = "N/A"
    if len(price_history) >= 2 and price_history[-2] > 0:
        daily_change_pct = (price_history[-1] - price_history[-2]) / price_history[-2] * 100.0
        daily_change_text = f"{daily_change_pct:+.2f}%"

    avg_entry_price = str(agent_state["avg"]) if agent_state["avg"] is not None else "None"
    return (
        f"Persona: {agent_state['persona']}. {PERSONA_PROMPTS[agent_state['persona']]}\n"
        f"Current price: {current_price:.6f} USDC ({daily_change_text})\n"
        f"Recent prices: {trend_text}\n"
        f"Event: {current_event['type']} magnitude={current_event['magnitude']:.3f}\n"
        f"News: {current_event['news']}\n"
        f"Last round market: buys={last_round_market['buys']} sells={last_round_market['sells']} holds={last_round_market['holds']}\n"
        f"Wallet: usdc={agent_state['usdc']:.6f}, token={agent_state['token']:.6f}, avg_entry={avg_entry_price}\n\n"
        "Reply exactly:\n"
        "ACTION: BUY or SELL or HOLD\n"
        "AMOUNT: number\n"
        "REASON: one short sentence\n\n"
        "BUY amount is USDC to spend. SELL amount is token to sell. HOLD amount must be 0."
    )


def parse_model_reply(reply_text):
    if not reply_text:
        return "HOLD", 0.0

    action = "HOLD"
    amount = 0.0
    for raw_line in reply_text.splitlines():
        line = raw_line.strip()
        upper_line = line.upper()
        if upper_line.startswith("ACTION:"):
            parsed_action = line.split(":", 1)[1].strip().upper()
            if parsed_action in {"BUY", "SELL", "HOLD"}:
                action = parsed_action
        elif upper_line.startswith("AMOUNT:"):
            try:
                amount = float(line.split(":", 1)[1].strip().replace(",", ""))
            except ValueError:
                amount = 0.0
    return action, max(0.0, amount)


async def request_agent_decision(http_session, request_semaphore, api_url, prompt_text, agent_index, retries, timeout_seconds):
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 64},
    }
    for attempt in range(retries + 1):
        try:
            async with request_semaphore:
                async with http_session.post(api_url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as response:
                    if response.status == 429:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    if response.status != 200:
                        body = (await response.text())[:500]
                        return agent_index, None, f"HTTP {response.status}: {body}"
                    response_json = await response.json()
            text = response_json["candidates"][0]["content"]["parts"][0]["text"].strip()
            return agent_index, text, None
        except Exception as error:  # noqa: BLE001
            if attempt < retries:
                await asyncio.sleep(1)
            else:
                return agent_index, None, str(error)
    return agent_index, None, "max retries"


def run_simulation(config):
    random_generator = random.Random(config["seed"])
    scenario_config = SCENARIOS[config["scenario"]]
    active_event_mix = scenario_config["events"] if scenario_config["events"] is not None else BASE_EVENT_MIX

    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is required.")

    gemini_api_url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{config['gemini_model']}:generateContent?key={GEMINI_API_KEY}"
    )

    pool_state = pool_new(config["pool_usdc"], config["pool_tokens"], config["fee_bps"])
    starting_price = pool_price(pool_state)
    persona_distribution = list(config["personas"].items())

    agents = []
    for agent_index in range(config["agents"]):
        selected_persona = pick_weighted_item(random_generator, persona_distribution)
        agents.append(
            {
                "id": f"Agent_{agent_index:04d}",
                "persona": selected_persona,
                "usdc": config["initial_agent_usdc"],
                "token": 0.0,
                "avg": None,
            }
        )

    initial_total_value = sum(agent["usdc"] + agent["token"] * starting_price for agent in agents)
    price_history = [starting_price]
    total_action_counts = {"BUY": 0, "SELL": 0, "HOLD": 0}
    round_snapshots = []
    market_counts_last_round = {"buys": 0, "sells": 0, "holds": len(agents)}

    async def run_all_rounds():
        request_semaphore = asyncio.Semaphore(max(1, config["max_concurrent"]))
        async with aiohttp.ClientSession() as http_session:
            for round_number in range(1, config["rounds"] + 1):
                event = sample_market_event(random_generator, active_event_mix)
                event["news"] = random_generator.choice(scenario_config["news"]) if scenario_config.get("news") else "No major news"
                current_pool_price = pool_price(pool_state)

                request_tasks = []
                for agent_index, agent in enumerate(agents):
                    prompt = build_model_prompt(agent, current_pool_price, price_history, event, market_counts_last_round)
                    request_tasks.append(
                        request_agent_decision(
                            http_session,
                            request_semaphore,
                            gemini_api_url,
                            prompt,
                            agent_index,
                            max(0, config["api_retries"]),
                            max(5, config["api_timeout_s"]),
                        )
                    )

                responses = await asyncio.gather(*request_tasks)
                decisions = []
                api_error_count = 0
                first_error = None
                for agent_index, response_text, error in responses:
                    if error:
                        api_error_count += 1
                        if first_error is None:
                            first_error = error
                        decisions.append((agent_index, "HOLD", 0.0))
                    else:
                        decisions.append((agent_index, *parse_model_reply(response_text)))
                if first_error:
                    logging.warning("round=%s api_errors=%s first_error=%s", round_number, api_error_count, first_error)

                round_buy_count = 0
                round_sell_count = 0
                round_hold_count = 0

                for agent_index, action, amount in decisions:
                    agent = agents[agent_index]
                    if action == "BUY" and amount > 0 and agent["usdc"] > 0:
                        usdc_to_spend = min(agent["usdc"], amount)
                        tokens_received = pool_buy(pool_state, usdc_to_spend)
                        if tokens_received > 0:
                            new_token_balance = agent["token"] + tokens_received
                            execution_price = pool_price(pool_state)
                            if agent["avg"] is None:
                                agent["avg"] = execution_price
                            else:
                                agent["avg"] = (
                                    (agent["avg"] * agent["token"] + execution_price * tokens_received) / new_token_balance
                                )
                            agent["usdc"] -= usdc_to_spend
                            agent["token"] = new_token_balance
                            round_buy_count += 1
                            total_action_counts["BUY"] += 1
                        else:
                            round_hold_count += 1
                            total_action_counts["HOLD"] += 1
                    elif action == "SELL" and amount > 0 and agent["token"] > 0:
                        tokens_to_sell = min(agent["token"], amount)
                        usdc_received = pool_sell(pool_state, tokens_to_sell)
                        if usdc_received > 0:
                            agent["token"] -= tokens_to_sell
                            agent["usdc"] += usdc_received
                            if agent["token"] <= 1e-12:
                                agent["token"] = 0.0
                                agent["avg"] = None
                            round_sell_count += 1
                            total_action_counts["SELL"] += 1
                        else:
                            round_hold_count += 1
                            total_action_counts["HOLD"] += 1
                    else:
                        round_hold_count += 1
                        total_action_counts["HOLD"] += 1

                market_counts_last_round.update(
                    {"buys": round_buy_count, "sells": round_sell_count, "holds": round_hold_count}
                )
                price_history.append(pool_price(pool_state))

                if config["log_every"] > 0 and (round_number % config["log_every"] == 0 or round_number == config["rounds"]):
                    round_snapshots.append(
                        {
                            "round": round_number,
                            "price": pool_price(pool_state),
                            "event_type": event["type"],
                            "event_magnitude": event["magnitude"],
                            "headline": event["news"],
                            "pool_usdc": pool_state["usdc"],
                            "pool_token": pool_state["token"],
                            "api_errors": api_error_count,
                        }
                    )
                    logging.info(
                        "round=%s price=%.6f event=%s mag=%.3f buys=%s sells=%s holds=%s errors=%s",
                        round_number,
                        pool_price(pool_state),
                        event["type"],
                        event["magnitude"],
                        round_buy_count,
                        round_sell_count,
                        round_hold_count,
                        api_error_count,
                    )

    asyncio.run(run_all_rounds())

    final_price = pool_price(pool_state)
    final_total_value = sum(agent["usdc"] + agent["token"] * final_price for agent in agents)
    final_agent_values = [agent["usdc"] + agent["token"] * final_price for agent in agents]
    final_agent_pnls = [value - config["initial_agent_usdc"] for value in final_agent_values]

    sorted_values = sorted(final_agent_values)
    sorted_pnls = sorted(final_agent_pnls)
    persona_counts = {persona: 0 for persona in PERSONAS}
    for agent in agents:
        persona_counts[agent["persona"]] = persona_counts.get(agent["persona"], 0) + 1

    result = {
        "config": dict(config),
        "scenario": config["scenario"],
        "starting_price": starting_price,
        "final_price": final_price,
        "initial_total_value_usdc": initial_total_value,
        "final_total_value_usdc": final_total_value,
        "pnl_total_usdc": final_total_value - initial_total_value,
        "persona_counts": persona_counts,
        "action_counts": total_action_counts,
        "agent_value_stats": {
            "min": min(final_agent_values),
            "p50": sorted_values[len(sorted_values) // 2],
            "max": max(final_agent_values),
            "avg": sum(final_agent_values) / len(final_agent_values),
        },
        "agent_pnl_stats": {
            "min": min(final_agent_pnls),
            "p50": sorted_pnls[len(sorted_pnls) // 2],
            "max": max(final_agent_pnls),
            "avg": sum(final_agent_pnls) / len(final_agent_pnls),
        },
        "time_series": round_snapshots,
    }

    if config["save_path"]:
        output_payload = dict(result)
        if config["save_agents"]:
            output_payload["agents"] = [
                {
                    "agent_id": agent["id"],
                    "persona": agent["persona"],
                    "usdc_balance": agent["usdc"],
                    "token_balance": agent["token"],
                    "avg_entry_price": agent["avg"],
                    "final_value_usdc": agent["usdc"] + agent["token"] * final_price,
                }
                for agent in agents
            ]
        with open(config["save_path"], "w", encoding="utf-8") as output_file:
            json.dump(output_payload, output_file, indent=2)
        logging.info("wrote results to %s", config["save_path"])

    return result


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config()
    result = run_simulation(config)
    logging.info(
        "done scenario=%s starting_price=%.6f final_price=%.6f pnl_total_usdc=%.2f",
        result["scenario"],
        result["starting_price"],
        result["final_price"],
        result["pnl_total_usdc"],
    )
    logging.info(
        "agent_value_p50=%.2f agent_pnl_p50=%.2f",
        result["agent_value_stats"]["p50"],
        result["agent_pnl_stats"]["p50"],
    )


if __name__ == "__main__":
    main()
