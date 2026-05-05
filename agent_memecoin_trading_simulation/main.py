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

# Add your own api key
GEMINI_API_KEY = "YOUR GEMINI API KEY HERE"

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


def build_agent_snapshots(agents, price, initial_agent_usdc):
    snapshots = []
    for agent in agents:
        value_usdc = agent["usdc"] + agent["token"] * price
        snapshots.append(
            {
                "agent_id": agent["id"],
                "persona": agent["persona"],
                "usdc_balance": agent["usdc"],
                "token_balance": agent["token"],
                "avg_entry_price": agent["avg"],
                "value_usdc": value_usdc,
                "pnl_usdc": value_usdc - initial_agent_usdc,
            }
        )
    return snapshots

def empty_action_counts():
    return {"BUY": 0, "SELL": 0, "HOLD": 0}


async def request_agent_decision(http_session, request_semaphore, api_url, prompt_text, agent_index, retries, timeout_seconds):
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 64},
    }
    for attempt in range(retries + 1):
        try:
            async with request_semaphore:
                async with http_session.post(api_url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as response:
                    if response.status in {429, 503}:
                        # Back off when rate-limited or service is temporarily unavailable.
                        backoff_seconds = min(8.0, float(2 ** attempt)) + random.random() * 0.35
                        await asyncio.sleep(backoff_seconds)
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
    total_action_counts = empty_action_counts()
    action_counts_by_persona = {persona: empty_action_counts() for persona in PERSONAS}
    market_counts_last_round = {"buys": 0, "sells": 0, "holds": len(agents)}
    rounds_output = []
    previous_avg_pnl_by_persona = {persona: 0.0 for persona in PERSONAS}

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
                round_errors = api_error_count
                round_action_counts_by_persona = {persona: empty_action_counts() for persona in PERSONAS}
                total_buy_tokens = 0.0
                total_sell_tokens = 0.0

                for agent_index, action, amount in decisions:
                    agent = agents[agent_index]
                    persona_action_counts = action_counts_by_persona[agent["persona"]]
                    round_persona_counts = round_action_counts_by_persona[agent["persona"]]
                    executed_action = "HOLD"
                    executed_usdc = 0.0
                    executed_token = 0.0
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
                            persona_action_counts["BUY"] += 1
                            round_persona_counts["BUY"] += 1
                            executed_action = "BUY"
                            executed_usdc = usdc_to_spend
                            executed_token = tokens_received
                            total_buy_tokens += tokens_received
                        else:
                            round_hold_count += 1
                            total_action_counts["HOLD"] += 1
                            persona_action_counts["HOLD"] += 1
                            round_persona_counts["HOLD"] += 1
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
                            persona_action_counts["SELL"] += 1
                            round_persona_counts["SELL"] += 1
                            executed_action = "SELL"
                            executed_usdc = usdc_received
                            executed_token = tokens_to_sell
                            total_sell_tokens += tokens_to_sell
                        else:
                            round_hold_count += 1
                            total_action_counts["HOLD"] += 1
                            persona_action_counts["HOLD"] += 1
                            round_persona_counts["HOLD"] += 1
                    else:
                        round_hold_count += 1
                        total_action_counts["HOLD"] += 1
                        persona_action_counts["HOLD"] += 1
                        round_persona_counts["HOLD"] += 1

                market_counts_last_round.update(
                    {"buys": round_buy_count, "sells": round_sell_count, "holds": round_hold_count}
                )
                round_price = pool_price(pool_state)
                price_history.append(round_price)
                round_pnl_by_persona = {persona: [] for persona in PERSONAS}
                for agent in agents:
                    agent_value = agent["usdc"] + agent["token"] * round_price
                    round_pnl_by_persona[agent["persona"]].append(agent_value - config["initial_agent_usdc"])

                round_persona_summary = {}
                for persona in PERSONAS:
                    persona_round_pnls = round_pnl_by_persona[persona]
                    persona_count = len(persona_round_pnls)
                    total_round_pnl = sum(persona_round_pnls) if persona_round_pnls else 0.0
                    avg_round_pnl = sum(persona_round_pnls) / len(persona_round_pnls) if persona_round_pnls else 0.0
                    prev_avg_pnl = previous_avg_pnl_by_persona[persona]
                    pnl_change = avg_round_pnl - prev_avg_pnl
                    pnl_change_pct = 0.0
                    if abs(prev_avg_pnl) > 1e-12:
                        pnl_change_pct = pnl_change / abs(prev_avg_pnl) * 100.0
                    round_persona_summary[persona] = {
                        "BUY": round_action_counts_by_persona[persona]["BUY"],
                        "SELL": round_action_counts_by_persona[persona]["SELL"],
                        "HOLD": round_action_counts_by_persona[persona]["HOLD"],
                        "count": persona_count,
                        "current_total_pnl": total_round_pnl,
                        "current_total_pnl_pct": (
                            total_round_pnl / (config["initial_agent_usdc"] * persona_count) * 100.0
                            if persona_count > 0 and config["initial_agent_usdc"] > 0
                            else 0.0
                        ),
                        "avg_pnl": avg_round_pnl,
                        "avg_pnl_pct": (avg_round_pnl / config["initial_agent_usdc"] * 100.0)
                        if config["initial_agent_usdc"] > 0
                        else 0.0,
                        "pnl_change_from_prev_round": pnl_change,
                        "pnl_change_pct_from_prev_round": pnl_change_pct,
                    }
                    previous_avg_pnl_by_persona[persona] = avg_round_pnl

                prev_price = price_history[-2]
                price_change_pct = 0.0
                if prev_price > 0:
                    price_change_pct = (round_price - prev_price) / prev_price * 100.0
                rounds_output.append(
                    {
                        "round": round_number,
                        "price": round_price,
                        "price_change_pct": price_change_pct,
                        "event": event["type"],
                        "news": event["news"],
                        "buys": round_buy_count,
                        "sells": round_sell_count,
                        "holds": round_hold_count,
                        "total_buy_tokens": total_buy_tokens,
                        "total_sell_tokens": total_sell_tokens,
                        "net_order_flow_tokens": total_buy_tokens - total_sell_tokens,
                        "by_persona": round_persona_summary,
                        "errors": round_errors,
                    }
                )

                if config["log_every"] > 0 and (round_number % config["log_every"] == 0 or round_number == config["rounds"]):
                    logging.info(
                        "round=%s price=%.6f event=%s mag=%.3f buys=%s sells=%s holds=%s errors=%s",
                        round_number,
                        round_price,
                        event["type"],
                        event["magnitude"],
                        round_buy_count,
                        round_sell_count,
                        round_hold_count,
                        api_error_count,
                    )

    asyncio.run(run_all_rounds())

    final_price = pool_price(pool_state)
    final_agent_snapshots = build_agent_snapshots(agents, final_price, config["initial_agent_usdc"])
    final_agent_values = [agent_snapshot["value_usdc"] for agent_snapshot in final_agent_snapshots]
    final_total_value = sum(final_agent_values)
    pnl_by_persona = {persona: [] for persona in PERSONAS}
    value_by_persona = {persona: [] for persona in PERSONAS}
    for agent_snapshot in final_agent_snapshots:
        persona = agent_snapshot["persona"]
        pnl_by_persona[persona].append(agent_snapshot["pnl_usdc"])
        value_by_persona[persona].append(agent_snapshot["value_usdc"])

    persona_stats = {}
    for persona in PERSONAS:
        persona_pnls = pnl_by_persona[persona]
        persona_values = value_by_persona[persona]
        if not persona_pnls:
            persona_stats[persona] = {
                "count": 0,
                "win_rate": 0.0,
                "avg_pnl": 0.0,
                "p50_pnl": 0.0,
                "avg_value": 0.0,
            }
            continue
        sorted_persona_pnls = sorted(persona_pnls)
        wins = sum(1 for pnl in persona_pnls if pnl > 0)
        persona_stats[persona] = {
            "count": len(persona_pnls),
            "win_rate": wins / len(persona_pnls),
            "avg_pnl": sum(persona_pnls) / len(persona_pnls),
            "p50_pnl": sorted_persona_pnls[len(sorted_persona_pnls) // 2],
            "avg_value": sum(persona_values) / len(persona_values),
        }

    result = {
        "config": {
            "agents": config["agents"],
            "rounds": config["rounds"],
            "seed": config["seed"],
            "scenario": config["scenario"],
            "fee_bps": config["fee_bps"],
            "pool_initial": {
                "usdc": config["pool_usdc"],
                "tokens": config["pool_tokens"],
                "price": starting_price,
            },
        },
        "market_summary": {
            "final_price": final_price,
            "total_pnl_usdc": final_total_value - initial_total_value,
            "action_totals": total_action_counts,
            "persona_performance": {
                persona: {
                    "count": persona_stats[persona]["count"],
                    "win_rate": persona_stats[persona]["win_rate"],
                    "avg_pnl": persona_stats[persona]["avg_pnl"],
                    "avg_pnl_pct": (
                        persona_stats[persona]["avg_pnl"] / config["initial_agent_usdc"] * 100.0
                        if config["initial_agent_usdc"] > 0
                        else 0.0
                    ),
                }
                for persona in PERSONAS
            },
        },
        "price_history": price_history,
        "rounds": rounds_output,
    }

    if config["save_path"]:
        with open(config["save_path"], "w", encoding="utf-8") as output_file:
            json.dump(result, output_file, indent=2)
        logging.info("wrote results to %s", config["save_path"])

    return result


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config()
    result = run_simulation(config)
    logging.info(
        "done scenario=%s starting_price=%.6f final_price=%.6f pnl_total_usdc=%.2f",
        result["config"]["scenario"],
        result["config"]["pool_initial"]["price"],
        result["market_summary"]["final_price"],
        result["market_summary"]["total_pnl_usdc"],
    )
if __name__ == "__main__":
    main()
