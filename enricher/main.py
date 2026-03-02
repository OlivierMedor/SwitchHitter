import os
import time
import logging
import psycopg2
from web3 import Web3
from dotenv import load_dotenv

# Set up simple logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

RPC_URL = os.getenv("ARBITRUM_RPC_URL")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER", "switchhitter")
DB_PASSWORD = os.getenv("DB_PASSWORD", "secretpassword")
DB_NAME = os.getenv("DB_NAME", "switchhitter")

# Arbitrum Aave V3 Pool Contract
AAVE_V3_POOL_ADDRESS = "0x794a61358D6845594F94dc1DB02A252b5b4814aD".lower()

# The first 4 bytes (8 hex characters) of the liquidationCall(address,address,address,uint256,bool) signature
LIQUIDATE_SIG = "0x00a718a9"

def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME
    )

def fetch_raw_liquidations(conn, limit=10):
    """Fetch unprocessed liquidations from the database."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, tx_hash, block_number, user_address 
            FROM liquidations 
            WHERE status = 'raw' 
            ORDER BY id ASC 
            LIMIT %s;
        """, (limit,))
        return cur.fetchall()

def enrich_liquidation(conn, w3, row_id, tx_hash, block_number, user_address):
    """Fetch the gas cost and count competitor attempts in the same block."""
    try:
        # 1. Get Gas Used by the winning transaction
        receipt = w3.eth.get_transaction_receipt(tx_hash)
        gas_used = receipt['gasUsed']
        effective_gas_price = receipt['effectiveGasPrice']
        total_gas_cost_wei = gas_used * effective_gas_price
        # Convert wei to ETH precision
        gas_cost_eth = float(w3.from_wei(total_gas_cost_wei, 'ether'))

        # 2. Find Competitor Attempts in the same block
        block = w3.eth.get_block(block_number, full_transactions=True)
        
        competitor_attempts = 0
        user_address_stripped = user_address.lower().replace("0x", "")

        for tx in block['transactions']:
            # Skip the winning transaction itself
            if tx['hash'].hex() == tx_hash:
                continue
                
            # Check if transaction was sent to the Aave V3 Pool
            to_addr = tx['to']
            if not to_addr or to_addr.lower() != AAVE_V3_POOL_ADDRESS:
                continue
                
            tx_input = tx['input'].hex() if isinstance(tx['input'], bytes) else tx['input']
            
            # Check if it was a liquidationCall and if the payload contained the user address
            if tx_input.startswith(LIQUIDATE_SIG) and user_address_stripped in tx_input.lower():
                competitor_attempts += 1

        # 3. Update the database record
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE liquidations 
                SET gas_used = %s, 
                    competitor_attempts = %s, 
                    status = 'enriched',
                    updated_at = NOW()
                WHERE id = %s;
            """, (gas_cost_eth, competitor_attempts, row_id))
            conn.commit()
            
        logger.info(f"Enriched ID {row_id} | Gas: {gas_cost_eth:.6f} ETH | Competitors: {competitor_attempts}")
        return True

    except Exception as e:
        import traceback
        logger.error(f"Failed to enrich liquidation {tx_hash}: {e}\n{traceback.format_exc()}")
        return False

def main():
    logger.info("Starting Enricher...")
    
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    try:
        head = w3.eth.block_number
        logger.info(f"Connected to Arbitrum. Chain tip: {head}")
    except Exception as e:
        logger.error(f"Failed to connect to Arbitrum RPC: {e}")
    
    conn = get_db_connection()
    
    while True:
        try:
            # We process small batches so we don't hold DB locks or hammer the RPC
            raw_rows = fetch_raw_liquidations(conn, limit=10)
            
            if not raw_rows:
                logger.info("No 'raw' liquidations found. Sleeping...")
                time.sleep(10)
                continue
                
            for row in raw_rows:
                row_id, tx_hash, block_number, user_address = row
                success = enrich_liquidation(conn, w3, row_id, tx_hash, block_number, user_address)
                
                # Sleep between each row to respect Alchemy's 330 CU / sec Free Tier Limit
                # get_transaction_receipt + get_block(full_transactions=True) is "expensive"
                time.sleep(1.5) 
                
        except Exception as e:
            logger.error(f"Error in enricher loop: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
