// SPDX-License-Identifier: MIT
pragma solidity ^0.8.10;

import "./interfaces/IPool.sol";
import "./interfaces/ISwapRouter.sol";

// Minimal ERC20 interface for the withdraw logic
interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);
}

contract SwitchHitter {
    address public owner;
    IPool public constant aavePool = IPool(0x794a61358D6845594F94dc1DB02A252b5b4814aD);
    ISwapRouter public constant uniswapRouter = ISwapRouter(0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45);
    ISwapRouter public constant sushiswapRouter = ISwapRouter(0x8A21F6768C1f807115200f86364024aD3c706dAA); // SushiSwap V3 Router placeholder

    modifier onlyOwner() {
        require(msg.sender == owner, "Not owner");
        _;
    }

    constructor() {
        owner = msg.sender;
    }

    // --- STRATEGY 1: PRIMARY LIQUIDATION ---
    function executePrimaryLiquidation(
        address debtAsset,
        address collateralAsset,
        address userToLiquidate,
        uint256 debtToCover
    ) external onlyOwner {
        
        // 1. Encode parameters so `executeOperation` knows which path to take
        bytes memory params = abi.encode(
            uint8(1), // Strategy 1
            collateralAsset,
            userToLiquidate,
            debtToCover
        );

        // 2. Request the FlashLoan from Aave V3
        // This will instantly call `executeOperation` on this contract
        aavePool.flashLoanSimple(
            address(this),
            debtAsset,
            debtToCover,
            params,
            0
        );
    }

    // --- STRATEGY 2: TOXIC SPLASH SCAVENGER (OPTION 2 ATOMIC ARB) ---
    function executeScavengerArb(
        address debtAsset, // Ususally USDC or WETH that we borrow
        address collateralAsset, // The crashed asset we want to buy on Uniswap
        uint256 flashloanAmount
    ) external onlyOwner {
        
        // 1. Encode parameters for Strategy 2
        bytes memory params = abi.encode(
            uint8(2), // Strategy 2
            collateralAsset,
            address(0), // No user to liquidate here
            flashloanAmount
        );

        // 2. Request the FlashLoan
        aavePool.flashLoanSimple(
            address(this),
            debtAsset,
            flashloanAmount,
            params,
            0
        );
    }

    // --- FLASHLOAN CALLBACK (THIS IS WHERE THE MAGIC HAPPENS) ---
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool) {
        require(msg.sender == address(aavePool), "Only Aave");
        require(initiator == address(this), "Only initiator");

        // Decode the parameters we packed earlier
        (uint8 strategy, address collateralAsset, address userToLiquidate, uint256 debtToCover) = abi.decode(params, (uint8, address, address, uint256));

        if (strategy == 1) {
            // -- EXECUTE LIQUIDATION --
            // 1. Approve Aave to spend the debtAsset we just borrowed
            IERC20(asset).approve(address(aavePool), amount);

            // 2. Execute the Liquidation Call natively on Aave
            aavePool.liquidationCall(
                collateralAsset,
                asset,           // debtAsset
                userToLiquidate,
                debtToCover,
                false            // We want the physical ERC20 collateral, not the aToken yield-bearing version
            );

            // 3. Sell the seized collateral on Uniswap V3 back to the debtAsset to repay the loan
            uint256 seizedCollateral = IERC20(collateralAsset).balanceOf(address(this));
            IERC20(collateralAsset).approve(address(uniswapRouter), seizedCollateral);
            
            ISwapRouter.ExactInputSingleParams memory swapParams = ISwapRouter.ExactInputSingleParams({
                tokenIn: collateralAsset,
                tokenOut: asset, // Swap back to the debtAsset we need to repay
                fee: 500, // Dynamic fee needed in production
                recipient: address(this),
                deadline: block.timestamp,
                amountIn: seizedCollateral,
                amountOutMinimum: 0, // Dynamic slippage needed in production
                sqrtPriceLimitX96: 0
            });
            uniswapRouter.exactInputSingle(swapParams);

        } else if (strategy == 2) {
            // -- EXECUTE SCAVENGER ATOMIC ARB --
            // 1. We just borrowed `amount` of debtAsset. Buy the crashed collateral on Uniswap V3.
            IERC20(asset).approve(address(uniswapRouter), amount);
            
            ISwapRouter.ExactInputSingleParams memory buyParams = ISwapRouter.ExactInputSingleParams({
                tokenIn: asset,
                tokenOut: collateralAsset,
                fee: 500,
                recipient: address(this),
                deadline: block.timestamp,
                amountIn: amount,
                amountOutMinimum: 0, 
                sqrtPriceLimitX96: 0
            });
            uint256 amountCollateralReceived = uniswapRouter.exactInputSingle(buyParams);

            // 2. Instantly sell that exact collateral on SushiSwap V3 for arbitrage
            IERC20(collateralAsset).approve(address(sushiswapRouter), amountCollateralReceived);

            ISwapRouter.ExactInputSingleParams memory sellParams = ISwapRouter.ExactInputSingleParams({
                tokenIn: collateralAsset,
                tokenOut: asset,
                fee: 500,
                recipient: address(this),
                deadline: block.timestamp,
                amountIn: amountCollateralReceived,
                amountOutMinimum: 0, 
                sqrtPriceLimitX96: 0
            });
            sushiswapRouter.exactInputSingle(sellParams);
        }

        // 4. Repay the Flashloan (Amount + Premium)
        // Aave will pull these funds from this contract at the end of the transaction
        uint256 amountToOwe = amount + premium;
        IERC20(asset).approve(address(aavePool), amountToOwe);

        // 5. The remaining balance of `asset` stays in this contract as PROFIT!
        return true;
    }

    // --- USER PROFIT EXTRACTION ---
    // At any point, the Owner can withdraw the stacked Arbitrage/Liquidation profits back to their personal wallet.
    function withdraw(address token) external onlyOwner {
        uint256 balance = IERC20(token).balanceOf(address(this));
        require(balance > 0, "No balance");
        IERC20(token).transfer(owner, balance);
    }
}
