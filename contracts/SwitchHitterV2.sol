// SPDX-License-Identifier: MIT
pragma solidity ^0.8.10;

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);
}

interface IBalancerVault {
    function flashLoan(
        address recipient,
        address[] memory tokens,
        uint256[] memory amounts,
        bytes memory userData
    ) external;
}

interface IPool {
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16 referralCode
    ) external;
    function liquidationCall(
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 debtToCover,
        bool receiveAToken
    ) external;
}

contract SwitchHitterV2 {
    address public owner;
    
    // Arbitrum Addresses
    IPool public constant aavePool = IPool(0x794a61358D6845594F94dc1DB02A252b5b4814aD);
    IBalancerVault public constant balancerVault = IBalancerVault(0xBA12222222228d8Ba445958a75a0704d566BF2C8);

    // Whitelisted DEX Aggregators
    mapping(address => bool) public approvedAggregators;

    modifier onlyOwner() {
        require(msg.sender == owner, "Not owner");
        _;
    }

    constructor() {
        owner = msg.sender;
        // Whitelist 1inch v5 Router on Arbitrum by default
        approvedAggregators[0x1111111254eb6c44bAC0beD2854e76F90643097d] = true;
    }

    // Allow owner to manage aggregators
    function setAggregator(address aggregator, bool approved) external onlyOwner {
        approvedAggregators[aggregator] = approved;
    }

    // --- MAIN ENTRY POINT ---
    // The Rust Execution Engine calls this function directly
    function triggerLiquidation(
        address targetUser,
        address debtAsset,          // e.g., USDC
        address collateralAsset,    // e.g., WBTC
        uint256 debtToCover,
        address aggregatorTarget,   // The 1inch Router address
        bytes calldata aggregatorData // Hex string containing the magic 1inch swap path
    ) external onlyOwner {
        
        // 1. Pack all the variables so they survive the flashloan callback jump
        bytes memory params = abi.encode(
            targetUser,
            debtAsset,
            collateralAsset,
            debtToCover,
            aggregatorTarget,
            aggregatorData
        );

        // 2. BALANCER FALLBACK LOGIC
        // Because Balancer holds all tokens in a single massive vault, checking its balance IS checking its liquidity
        uint256 balancerLiquidity = IERC20(debtAsset).balanceOf(address(balancerVault));
        
        if (balancerLiquidity >= debtToCover) {
            // -- PRIMARY ROUTE: BALANCER (0% FEE) --
            address[] memory tokens = new address[](1);
            tokens[0] = debtAsset;
            uint256[] memory amounts = new uint256[](1);
            amounts[0] = debtToCover;
            
            balancerVault.flashLoan(
                address(this),
                tokens,
                amounts,
                params
            );
        } else {
            // -- FALLBACK ROUTE: AAVE V3 (0.05% FEE) --
            // If Balancer is drained, we guarantee execution by borrowing from Aave directly
            aavePool.flashLoanSimple(
                address(this),
                debtAsset,
                debtToCover,
                params,
                0
            );
        }
    }

    // --- FLASHLOAN CALLBACKS ---

    // 1. Balancer Callback
    function receiveFlashLoan(
        address[] memory tokens,
        uint256[] memory amounts,
        uint256[] memory feeAmounts,
        bytes memory userData
    ) external {
        require(msg.sender == address(balancerVault), "Only Balancer");
        
        uint256 amountToRepay = amounts[0] + feeAmounts[0]; // Balancer fee is exactly 0
        
        _executeLiquidationAndSwap(tokens[0], amounts[0], userData);
        
        // Ensure profitability / enough to repay before transferring
        require(IERC20(tokens[0]).balanceOf(address(this)) >= amountToRepay, "NOT_ENOUGH_TO_REPAY");

        // Transfer Balancer repayment (Balancer requires pull via transfer, not approve)
        IERC20(tokens[0]).transfer(address(balancerVault), amountToRepay);
    }

    // 2. Aave Callback
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool) {
        require(msg.sender == address(aavePool), "Only Aave");
        require(initiator == address(this), "Only initiator");

        uint256 amountToRepay = amount + premium; // Aave fee is 0.05%

        _executeLiquidationAndSwap(asset, amount, params);

        // Ensure profitability / enough to repay before returning
        require(IERC20(asset).balanceOf(address(this)) >= amountToRepay, "NOT_ENOUGH_TO_REPAY");

        // Approve Aave to pull the repayment (Aave pulls via transferFrom)
        IERC20(asset).approve(address(aavePool), 0);
        IERC20(asset).approve(address(aavePool), amountToRepay);
        return true;
    }

    // --- CORE EXECUTION LOGIC ---
    function _executeLiquidationAndSwap(
        address debtAsset,
        uint256 flashloanAmount,
        bytes memory params
    ) internal {
        (
            address targetUser,
            , // debtAsset is already passed gracefully
            address collateralAsset,
            uint256 debtToCover,
            address aggregatorTarget,
            bytes memory aggregatorData
        ) = abi.decode(params, (address, address, address, uint256, address, bytes));

        // 1. Approve Aave to spend the debtAsset we just borrowed
        IERC20(debtAsset).approve(address(aavePool), 0);
        IERC20(debtAsset).approve(address(aavePool), flashloanAmount);

        // 2. Execute the physical liquidation!
        aavePool.liquidationCall(
            collateralAsset,
            debtAsset,
            targetUser,
            debtToCover,
            false // Receive physical tokens, not aTokens
        );

        // 3. Measure how much collateral we successfully seized
        uint256 seizedCollateral = IERC20(collateralAsset).balanceOf(address(this));
        require(seizedCollateral > 0, "Liquidation yielded zero collateral");

        require(approvedAggregators[aggregatorTarget], "Aggregator not whitelisted");

        // 4. Approve the 1inch Router to take our seized collateral
        IERC20(collateralAsset).approve(aggregatorTarget, 0);
        IERC20(collateralAsset).approve(aggregatorTarget, seizedCollateral);

        // 5. Execute the blind 1inch aggregator routing!
        // We pass the raw hex calldata 1inch gave us directly to the blockchain
        (bool success, ) = aggregatorTarget.call(aggregatorData);
        require(success, "Aggregator swap failed - Slippage exceeded");

        // Flashloan Repayment happens automatically after this function.
        // Whatever is left over after repaying the loan is pure profit!
    }

    // --- PROFIT ROUTING ---
    // Instead of holding dead cash, route profits intelligently
    function routeProfits(address token, address targetYieldVault) external onlyOwner {
        uint256 profit = IERC20(token).balanceOf(address(this));
        require(profit > 0, "No profit to route");

        // Rule: 20% to cold wallet, 80% to yield vault (e.g., Aave depositing)
        uint256 coldWalletShare = (profit * 20) / 100;
        uint256 yieldShare = profit - coldWalletShare;

        // 1. Send the safe baseline profit home
        IERC20(token).transfer(owner, coldWalletShare);

        // 2. Put the rest of the money to work earning passive yield
        IERC20(token).approve(targetYieldVault, yieldShare);
        // Note: Actual deposit logic depends on target protocol (Aave vs Yearn). 
        // For Aave: IPool(targetYieldVault).supply(token, yieldShare, address(this), 0);
        
        // Simulating the transfer to a known yield vault for now
        IERC20(token).transfer(targetYieldVault, yieldShare);
    }
    
    // Required to receive unwrapped ETH from aggregator swaps
    receive() external payable {}
}
