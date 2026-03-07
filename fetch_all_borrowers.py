import requests
import json
import os

GRAPH_URL = "https://api.thegraph.com/subgraphs/name/messari/aave-v3-arbitrum"

print("=========================================")
print(" 🚀 FETCHING ALL ACTIVE AAVE BORROWERS 🚀")
print("=========================================\n")

query = """
{
  positions(
    where: {side: BORROW, balance_gt: "0"}
    first: 1000
    orderBy: balance
    orderDirection: desc
  ) {
    account {
      id
    }
    balance
  }
}
"""

try:
    print("Fetching active borrow positions from The Graph...")
    response = requests.post(GRAPH_URL, json={'query': query})
    data = response.json()
    
    positions = data.get('data', {}).get('positions', [])
    unique_borrowers = list(set([p['account']['id'] for p in positions]))
    
    print(f"✅ Found {len(unique_borrowers)} unique accounts currently borrowing assets.")
    
    output_path = os.path.join("engine", "active_borrowers.json")
    with open(output_path, "w") as f:
        json.dump(unique_borrowers, f, indent=2)
        
    print(f"\nSaved transparently to {output_path}!")
    print("The Rust Engine can now load this instantly on startup (0 API calls).")

except Exception as e:
    print(f"❌ Error fetching from subgraph: {e}")
