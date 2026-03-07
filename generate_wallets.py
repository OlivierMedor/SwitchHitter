import secrets

print("=========================================")
print("   🚀 GENERATING 10 HOT WALLETS 🚀       ")
print("=========================================\n")

env_output = "\n# --- BOT WALLET FLEET ---\n"
private_keys = []

for i in range(1, 11):
    # An Ethereum private key is just 32 bytes of secure random entropy!
    # No external libraries needed.
    priv_key = secrets.token_hex(32)
    private_keys.append((i, priv_key))
    env_output += f"BOT_WALLET_{i}={priv_key}\n"

print("✅ Private Keys generated successfully!\n")

print("1️⃣  Paste these private keys into MetaMask (or Rabby) using 'Import Account':")
print("     (MetaMask will calculate the public 0x... address for you automatically)")
print("-----------------------------------------")
for i, key in private_keys:
    print(f"Wallet {i:2d} Private Key: {key}")

print("\n\n2️⃣  Once you fund them with $5 each, paste this exact block into engine/.env:")
print("-----------------------------------------")
print(env_output)
print("=========================================\n")
