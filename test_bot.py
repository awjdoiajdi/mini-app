import hashlib
import hmac
import json
import os
from urllib.parse import urlencode

os.environ.setdefault("BOT_TOKEN", "123456:test-token-for-local-check")

from bot import validate_init_data


def signed_data(token, now=1_700_000_000):
    values = {"auth_date": str(now), "query_id": "test", "user": json.dumps({"id": 42})}
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


if __name__ == "__main__":
    token = os.environ["BOT_TOKEN"]
    data = signed_data(token)
    assert validate_init_data(data, token, now=1_700_000_000)["id"] == 42
    try:
        validate_init_data(data.replace("test", "fake"), token, now=1_700_000_000)
        raise AssertionError("tampered data was accepted")
    except ValueError:
        pass
    print("Telegram initData validation: OK")
