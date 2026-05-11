"""Institutional strategy pods for BTC/USD, XAU/USD, NIFTY 50.

Pods are now BlackRock-tier sized (Phase 3 cluster-aware additions):
  XAU pod    — 9 strategies (Citadel HFT mean-rev, Renaissance HMM,
               JPM macro flow w/ real yields, D.E. Shaw stat-arb,
               Goldman trend-VWAP, JPM order-flow stop-run, Aladdin
               VWAP-bandit, Aladdin/RiskMetrics IV/HV, Investopedia scalp).
  NIFTY pod  — 12 strategies (Phase-3 adds BANKNIFTY lead-lag).
  BTC pod    — 7 strategies (Phase-3 adds CARRY/funding-skew).
"""
from strategies.base import StrategyAgent, StrategyVote

# XAU base pod (5)
from strategies.cointegration import CointegrationStrategy
from strategies.macro_flow import MacroFlowStrategy
from strategies.microstructure import MicrostructureStrategy
from strategies.momentum_macro import MomentumMacroStrategy
from strategies.regime_hmm import RegimeHMMStrategy

# NIFTY base pod (5)
from strategies.nifty_fii_dii_flow import NIFTYFiiDiiFlowStrategy
from strategies.nifty_microstructure import NIFTYMicrostructureStrategy
from strategies.nifty_options_oi import NIFTYOptionsOIStrategy
from strategies.nifty_pairs_arb_bn import NIFTYPairsArbStrategy
from strategies.nifty_regime_hmm import NIFTYRegimeHMMStrategy

# BlackRock-tier additions (Phase 4)
from strategies.btc_microstructure import BTCMicrostructureStrategy
from strategies.greeks_proxy import GreeksProxyStrategy
from strategies.oi_crossover import OICrossoverStrategy
from strategies.orderflow_liquidity import OrderflowLiquidityStrategy
from strategies.scalp_indicators import ScalpIndicatorsStrategy
from strategies.session_volume import SessionVolumeStrategy
from strategies.volatility_regime import VolatilityRegimeStrategy
from strategies.vwap_bandit import VWAPBanditStrategy

# Phase-3 cluster-aware additions
from strategies.btc_funding_skew import BTCFundingSkewStrategy
from strategies.nifty_bn_lead import NiftyBNLeadStrategy

__all__ = [
    "StrategyAgent",
    "StrategyVote",
    # XAU pod
    "CointegrationStrategy",
    "MacroFlowStrategy",
    "MicrostructureStrategy",
    "MomentumMacroStrategy",
    "RegimeHMMStrategy",
    "default_pod",
    # NIFTY pod
    "NIFTYFiiDiiFlowStrategy",
    "NIFTYMicrostructureStrategy",
    "NIFTYOptionsOIStrategy",
    "NIFTYPairsArbStrategy",
    "NIFTYRegimeHMMStrategy",
    "default_nifty_pod",
    # BlackRock additions
    "BTCMicrostructureStrategy",
    "GreeksProxyStrategy",
    "OICrossoverStrategy",
    "OrderflowLiquidityStrategy",
    "ScalpIndicatorsStrategy",
    "SessionVolumeStrategy",
    "VolatilityRegimeStrategy",
    "VWAPBanditStrategy",
    "default_btc_pod",
    # Phase-3 cluster-aware
    "BTCFundingSkewStrategy",
    "NiftyBNLeadStrategy",
]


def default_pod() -> list[StrategyAgent]:
    """XAU/USD pod — 9 institutional strategies."""
    return [
        # Base 5 (Phases 1–2)
        MicrostructureStrategy(),
        RegimeHMMStrategy(),
        MacroFlowStrategy(),
        CointegrationStrategy(),
        MomentumMacroStrategy(),
        # BlackRock-tier additions (Phase 4)
        OrderflowLiquidityStrategy(asset="XAU"),
        VWAPBanditStrategy(asset="XAU"),
        VolatilityRegimeStrategy(asset="XAU"),
        ScalpIndicatorsStrategy(asset="XAU"),
        # SessionVolumeStrategy(asset="XAU"),  # XAU yfinance feed gives volume=0 most of the time
    ]


def default_nifty_pod() -> list[StrategyAgent]:
    """NIFTY 50 pod — 12 institutional strategies (Phase-3: +BANKNIFTY lead-lag)."""
    return [
        # Base 5 (Phase 3)
        NIFTYMicrostructureStrategy(),
        NIFTYRegimeHMMStrategy(),
        NIFTYFiiDiiFlowStrategy(),
        NIFTYPairsArbStrategy(),
        NIFTYOptionsOIStrategy(),
        # BlackRock-tier additions (Phase 4)
        OrderflowLiquidityStrategy(asset="NIFTY"),
        VWAPBanditStrategy(asset="NIFTY"),
        VolatilityRegimeStrategy(asset="NIFTY"),
        ScalpIndicatorsStrategy(asset="NIFTY"),
        OICrossoverStrategy(),
        GreeksProxyStrategy(),
        # Phase-3 cluster-aware: BANKNIFTY lead-lag (FLOW cluster)
        NiftyBNLeadStrategy(),
    ]


def default_btc_pod() -> list[StrategyAgent]:
    """BTC/USD pod — 7 institutional strategies (Phase-3: +funding-skew CARRY)."""
    return [
        BTCMicrostructureStrategy(),
        OrderflowLiquidityStrategy(asset="BTC"),
        VWAPBanditStrategy(asset="BTC"),
        ScalpIndicatorsStrategy(asset="BTC"),
        SessionVolumeStrategy(asset="BTC"),
        RegimeHMMStrategy(),  # asset-agnostic — runs on BTC 1h returns
        # Phase-3 cluster-aware: funding-skew CARRY cluster
        BTCFundingSkewStrategy(),
    ]
