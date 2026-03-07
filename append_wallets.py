import secrets
import os

env_path = os.path.join("engine", ".env")

with open(env_path, "a", encoding="utf-8") as f:
    f.write("\n# --- BOT WALLET FLEET ---\n")
    for i in range(1, 11):
        priv_key = secrets.token_hex(32)
        f.write(f"BOT_WALLET_{i}={priv_key}\n")

print(f"✅ Appended 10 wallets directly to {env_path}")
