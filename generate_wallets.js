const { ethers } = require("ethers");
const fs = require("fs");

console.log("=========================================");
console.log("   🚀 GENERATING 10 HOT WALLETS 🚀       ");
console.log("=========================================\n");

let envOutput = "\n# --- BOT WALLET FLEET ---\n";
let addressList = [];

for (let i = 1; i <= 10; i++) {
    const wallet = ethers.Wallet.createRandom();

    // Store for console display
    addressList.push({ id: i, address: wallet.address });

    // Format for rust engine .env (strip the 0x prefix from private key)
    envOutput += `BOT_WALLET_${i}=${wallet.privateKey.slice(2)}\n`;
}

console.log("✅ Wallets generated successfully!\n");

console.log("1️⃣  Copy these addresses and send $5 of ETH on Arbitrum to each:");
console.log("-----------------------------------------");
addressList.forEach(w => {
    console.log(`Wallet ${w.id.toString().padStart(2, ' ')}: ${w.address}`);
});

console.log("\n\n2️⃣  Paste this exact block into your engine/.env file:");
console.log("-----------------------------------------");
console.log(envOutput);
console.log("=========================================\n");
