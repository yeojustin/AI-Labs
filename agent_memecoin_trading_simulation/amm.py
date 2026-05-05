def pool_new(usdc, token, fee_bps):
    if usdc <= 0 or token <= 0:
        raise ValueError("pool reserves must be positive")
    return {"usdc": float(usdc), "token": float(token), "fee_bps": float(fee_bps), "k": float(usdc * token)}


def pool_price(pool):
    return pool["usdc"] / pool["token"]


def _fee_mult(fee_bps):
    return 1.0 - fee_bps / 10000.0


def pool_buy(pool, usdc_in):
    if usdc_in <= 0:
        return 0.0
    effective = usdc_in * _fee_mult(pool["fee_bps"])
    new_usdc = pool["usdc"] + effective
    new_token = pool["k"] / new_usdc
    token_out = pool["token"] - new_token
    if token_out <= 0:
        return 0.0
    pool["usdc"] = new_usdc
    pool["token"] = new_token
    return token_out


def pool_sell(pool, token_in):
    if token_in <= 0:
        return 0.0
    effective = token_in * _fee_mult(pool["fee_bps"])
    new_token = pool["token"] + effective
    new_usdc = pool["k"] / new_token
    usdc_out = pool["usdc"] - new_usdc
    if usdc_out <= 0:
        return 0.0
    pool["token"] = new_token
    pool["usdc"] = new_usdc
    return usdc_out
