"""market_factors.py - what moves each stock or coin: its sector or category, the drivers behind it, measured on data.

Owner, 2026-11-16: "Việc phân tích thị trường liên quan đến loại mã hàng (mã coin) trong lĩnh vực nào? những gì ảnh
hưởng đến mã đó, cả thế giới và việt nam. Phải phân tích chuyên sâu thì mới có hướng đi đúng."

Three layers per group (a Vietnamese sector, a group of world stocks, a crypto category):
  1. Knowledge: the drivers a group is known to follow (the US dollar, US 10-year yield, oil, copper, China, the
     Nasdaq, Bitcoin...), each with the expected direction and why, plus the drivers no price feed covers
     (credit growth, FDI, ETF flows...), listed as things to watch.
  2. Measurement: the correlation and beta of the group's own equal-weight index with each driver, on daily returns
     over the dates both trade (up to 250). A known driver the data does not confirm is flagged, not hidden.
  3. State now: each driver's trend (price against its 50-day average and its one-month change). A driver moving
     the way that helps the group is a tailwind, the other way a headwind. The group's factor score (-100..100) is
     the average over its drivers; it uses the expected signs only, so the same score can be rebuilt for any past
     day without looking ahead (`score_at`), which lets the weekly review test whether it predicts anything.

Plus relative strength: the group index against the market benchmark over one and three months.
Driver prices come from the bars archive (market_store), which the Markets page fills for all three markets.
Blocking sqlite: run in a thread.
"""
from __future__ import annotations

import bisect
import math
import threading
import time

# ---------------------------------------------------------------- knowledge

# Driver id -> (archive kind, symbol). Names come from market_data.WORLD or the label here.
DRIVERS = {
    "usd": ("world", "DX-Y.NYB"), "usdvnd": ("world", "USDVND=X"), "usdjpy": ("world", "USDJPY=X"),
    "usdcny": ("world", "USDCNY=X"), "us10y": ("world", "^TNX"), "vix": ("world", "^VIX"),
    "sp500": ("world", "^GSPC"), "nasdaq": ("world", "^IXIC"), "brent": ("world", "BZ=F"), "wti": ("world", "CL=F"),
    "gas": ("world", "NG=F"), "copper": ("world", "HG=F"), "gold": ("world", "GC=F"), "china": ("world", "000001.SS"),
    "hangseng": ("world", "^HSI"), "nikkei": ("world", "^N225"), "nvidia": ("world", "NVDA"),
    "vnindex": ("vn", "VNINDEX"), "bitcoin": ("crypto", "bitcoin"),
}
DRIVER_NAMES = {
    "usd": ("Chỉ số USD (DXY)", "US Dollar Index (DXY)"), "usdvnd": ("Tỷ giá USD/VND", "USD/VND"),
    "usdjpy": ("Tỷ giá USD/JPY", "USD/JPY"), "usdcny": ("Tỷ giá USD/CNY", "USD/CNY"),
    "us10y": ("Lợi suất trái phiếu Mỹ 10 năm", "US 10-year yield"), "vix": ("Chỉ số sợ hãi VIX", "VIX fear index"),
    "sp500": ("S&P 500", "S&P 500"), "nasdaq": ("Nasdaq", "Nasdaq"), "brent": ("Dầu Brent", "Brent oil"),
    "wti": ("Dầu WTI", "WTI oil"), "gas": ("Khí tự nhiên", "Natural gas"), "copper": ("Giá đồng", "Copper"),
    "gold": ("Giá vàng", "Gold"), "china": ("Chứng khoán Thượng Hải", "Shanghai Composite"),
    "hangseng": ("Hang Seng", "Hang Seng"), "nikkei": ("Nikkei 225", "Nikkei 225"), "nvidia": ("Cổ phiếu Nvidia", "Nvidia stock"),
    "vnindex": ("VN-Index", "VN-Index"), "bitcoin": ("Bitcoin", "Bitcoin"),
}

# (driver, expected sign, why vi, why en). +1: the group tends to rise when the driver rises.
_VN_COMMON = [
    ("usd", -1, "USD mạnh lên thường kéo vốn ngoại rút khỏi thị trường mới nổi như Việt Nam",
     "A stronger dollar tends to pull foreign money out of emerging markets like Vietnam"),
    ("vix", -1, "Thế giới hoảng sợ (VIX cao) thì khối ngoại thường bán ròng",
     "When global fear rises (high VIX), foreign investors tend to sell"),
]
GROUPS = {
    "vn": {
        "bank": ([("vnindex", 1, "Ngân hàng chiếm tỷ trọng lớn nhất, đi cùng thị trường chung",
                   "Banks are the largest weight and move with the market"),
                  ("usdvnd", -1, "Tỷ giá căng thì Ngân hàng Nhà nước hút tiền về, lãi suất liên ngân hàng tăng",
                   "Pressure on the dong makes the central bank drain liquidity and lift interbank rates"),
                  ("us10y", -1, "Lãi suất Mỹ cao giới hạn dư địa hạ lãi suất trong nước",
                   "High US rates limit room for lower rates at home")] + _VN_COMMON,
                 [("Tăng trưởng tín dụng và hạn mức tín dụng", "Credit growth and credit quotas"),
                  ("Biên lãi ròng (NIM) và chi phí vốn", "Net interest margin and funding costs"),
                  ("Nợ xấu và trích lập dự phòng", "Bad debt and provisions"),
                  ("Lãi suất điều hành của Ngân hàng Nhà nước", "Central bank policy rates")]),
        "broker": ([("vnindex", 1, "Doanh thu môi giới, cho vay margin và tự doanh đều ăn theo thị trường",
                     "Brokerage, margin lending and proprietary trading all ride the market")] + _VN_COMMON,
                   [("Thanh khoản toàn thị trường", "Market trading volume"),
                    ("Nâng hạng thị trường (FTSE, MSCI)", "Market upgrade (FTSE, MSCI)"),
                    ("Dư nợ margin", "Margin debt")]),
        "realestate": ([("us10y", -1, "Lãi suất cao làm vay mua nhà và vay phát triển dự án đắt hơn",
                         "Higher rates make mortgages and project loans dearer"),
                        ("usdvnd", -1, "Tỷ giá căng kéo lãi suất trong nước lên", "Pressure on the dong pushes local rates up"),
                        ("vnindex", 1, "Cổ phiếu bất động sản có độ nhạy cao với thị trường chung",
                         "Property stocks are highly sensitive to the market")] + _VN_COMMON,
                       [("Pháp lý dự án và tốc độ cấp phép", "Project approvals and legal status"),
                        ("Lãi suất cho vay mua nhà", "Mortgage rates"),
                        ("Trái phiếu doanh nghiệp đáo hạn", "Corporate bonds coming due")]),
        "indpark": ([("sp500", 1, "Kinh tế Mỹ khỏe thì đơn hàng xuất khẩu và vốn FDI vào nhà máy tăng",
                      "A strong US economy lifts export orders and factory FDI"),
                     ("copper", 1, "Giá đồng phản ánh chu kỳ công nghiệp toàn cầu", "Copper tracks the global industrial cycle")]
                    + _VN_COMMON,
                    [("Vốn FDI đăng ký và giải ngân", "FDI pledged and disbursed"),
                     ("Thuế quan Mỹ với hàng Việt Nam", "US tariffs on Vietnamese goods"),
                     ("Xu hướng dịch chuyển sản xuất khỏi Trung Quốc", "Supply chains moving out of China")]),
        "materials": ([("copper", 1, "Kim loại công nghiệp lên giá thì thép, hóa chất cũng thuận chiều",
                        "When industrial metals rise, steel and chemicals tend to follow"),
                       ("china", 1, "Trung Quốc quyết định giá thép, quặng sắt và hóa chất toàn cầu",
                        "China sets global prices for steel, iron ore and chemicals")] + _VN_COMMON,
                      [("Giá thép cuộn cán nóng (HRC) và quặng sắt", "Hot-rolled coil and iron ore prices"),
                       ("Giá phân bón, phốt pho, cao su", "Fertiliser, phosphorus and rubber prices"),
                       ("Đầu tư công và xây dựng dân dụng", "Public investment and housing construction")]),
        "energy": ([("brent", 1, "Doanh thu dầu khí đi theo giá dầu", "Oil and gas revenue follows the oil price"),
                    ("gas", 1, "Giá khí ảnh hưởng trực tiếp doanh thu ngành khí", "Gas prices drive gas distributors' revenue")]
                   + _VN_COMMON,
                   [("Giá xăng dầu điều hành trong nước", "Regulated fuel prices at home"),
                    ("Quy hoạch điện VIII và giá điện", "Power development plan and power prices"),
                    ("Dự án dầu khí lớn (Lô B - Ô Môn)", "Large gas projects (Block B)")]),
        "consumer": ([("usdvnd", -1, "Nguyên liệu nhập khẩu đắt hơn và sức mua giảm khi tỷ giá tăng",
                       "A weaker dong makes imported inputs dearer and squeezes spending"),
                      ("vnindex", 1, "Tâm lý thị trường chung", "General market mood")] + _VN_COMMON,
                     [("Tổng mức bán lẻ và thu nhập người dân", "Retail sales and household income"),
                      ("Giá vàng (trang sức) và giá sữa bột (sữa)", "Gold price (jewellery), milk powder (dairy)")]),
        "tech": ([("nasdaq", 1, "Định giá ngành công nghệ toàn cầu kéo theo", "Global tech valuations carry the sector"),
                  ("usdvnd", 1, "Doanh thu xuất khẩu phần mềm thu bằng USD", "Software export revenue is earned in dollars"),
                  ("usdjpy", -1, "Yên yếu làm doanh thu từ Nhật quy đổi ra ít hơn",
                   "A weak yen shrinks revenue from Japan once converted")] + _VN_COMMON,
                 [("Chi tiêu công nghệ thông tin của Mỹ, Nhật", "IT spending in the US and Japan"),
                  ("Hợp đồng chuyển đổi số và AI", "Digital transformation and AI contracts")]),
        "transport": ([("brent", -1, "Nhiên liệu là chi phí lớn nhất của hàng không, vận tải", "Fuel is the largest cost for airlines and shipping"),
                       ("sp500", 1, "Thương mại toàn cầu tốt thì hàng qua cảng tăng", "Strong global trade lifts port volumes")]
                      + _VN_COMMON,
                      [("Khách du lịch quốc tế", "International tourist arrivals"),
                       ("Cước container và công suất cảng", "Container rates and port capacity")]),
        "construction": ([("us10y", -1, "Lãi suất cao làm chậm dự án và tăng chi phí vốn", "High rates slow projects and raise funding costs"),
                          ("copper", -1, "Giá vật liệu đầu vào tăng ăn vào biên lợi nhuận", "Rising material prices eat into margins")]
                         + _VN_COMMON,
                         [("Giải ngân đầu tư công", "Public investment disbursement"),
                          ("Giá thép, xi măng, cát đá", "Steel, cement and aggregate prices")]),
        "agri": ([("usdvnd", 1, "Doanh nghiệp xuất khẩu thu USD, tỷ giá tăng là lợi", "Exporters earn dollars and gain from a weaker dong"),
                  ("sp500", 1, "Nhu cầu tiêu dùng Mỹ, thị trường chính của thủy sản", "US consumer demand, the main seafood market")]
                 + _VN_COMMON,
                 [("Thuế chống bán phá giá và rào cản thương mại", "Anti-dumping duties and trade barriers"),
                  ("Giá cá tra, giá gạo, thời tiết", "Pangasius and rice prices, weather")]),
        "insurance": ([("us10y", 1, "Lãi suất cao nâng lợi suất danh mục đầu tư trái phiếu", "Higher rates lift the yield on bond portfolios"),
                       ("vnindex", 1, "Danh mục cổ phiếu đầu tư đi theo thị trường", "Equity holdings move with the market")]
                      + _VN_COMMON,
                      [("Phí bảo hiểm khai thác mới", "New premiums written"),
                       ("Quy định ngành bảo hiểm", "Insurance regulation")]),
        "other": ([("vnindex", 1, "Đi theo thị trường chung", "Moves with the market")] + _VN_COMMON, []),
    },
    "world": {
        "semis": ([("nasdaq", 1, "Chip là trụ cột của chỉ số công nghệ", "Chips are a pillar of the tech index"),
                   ("us10y", -1, "Lãi suất cao làm giảm định giá cổ phiếu tăng trưởng", "Higher rates cut the value of growth stocks")],
                  [("Chi tiêu đầu tư AI của các hãng điện toán đám mây", "AI capital spending by cloud providers"),
                   ("Kiểm soát xuất khẩu chip sang Trung Quốc", "Chip export controls on China"),
                   ("Chu kỳ tồn kho bộ nhớ", "The memory inventory cycle")]),
        "bigtech": ([("nasdaq", 1, "Đi cùng chỉ số công nghệ", "Moves with the tech index"),
                     ("us10y", -1, "Lãi suất cao làm giảm định giá cổ phiếu tăng trưởng", "Higher rates cut the value of growth stocks")],
                    [("Kết quả kinh doanh quý", "Quarterly earnings"), ("Quảng cáo số và điện toán đám mây", "Digital ads and cloud"),
                     ("Quy định chống độc quyền", "Antitrust rules")]),
        "financials": ([("us10y", 1, "Lãi suất cao mở rộng biên lãi của ngân hàng", "Higher rates widen bank margins"),
                        ("vix", -1, "Thị trường hoảng loạn làm tăng rủi ro tín dụng", "Market stress raises credit risk")],
                       [("Chênh lệch lãi suất dài hạn và ngắn hạn", "The gap between long and short rates"),
                        ("Tín dụng tiêu dùng và nợ xấu", "Consumer credit and bad loans")]),
        "energy": ([("wti", 1, "Lợi nhuận đi theo giá dầu", "Profits follow the oil price")],
                   [("Quyết định sản lượng của OPEC+", "OPEC+ output decisions"), ("Tồn kho dầu Mỹ", "US crude inventories")]),
        "consumer": ([("sp500", 1, "Đi cùng thị trường chung", "Moves with the market"),
                      ("us10y", -1, "Lãi suất cao làm chậm chi tiêu tiêu dùng", "High rates slow consumer spending")],
                     [("Chi tiêu tiêu dùng và lạm phát Mỹ", "US consumer spending and inflation"),
                      ("Cạnh tranh xe điện từ Trung Quốc (Tesla)", "Chinese EV competition (Tesla)")]),
        "health": ([("sp500", 1, "Đi cùng thị trường chung", "Moves with the market")],
                   [("Thuốc giảm cân GLP-1 và kết quả thử nghiệm", "GLP-1 weight-loss drugs and trial results"),
                    ("Chính sách giá thuốc của Mỹ", "US drug pricing policy")]),
        "crypto_stock": ([("bitcoin", 1, "Doanh thu sàn đi theo giá và khối lượng giao dịch tiền số", "Exchange revenue follows crypto prices and volume")],
                         [("Quy định của SEC với tiền số", "SEC rules on crypto")]),
        "china": ([("hangseng", 1, "Đi cùng thị trường Hồng Kông", "Moves with the Hong Kong market"),
                   ("usdcny", -1, "Nhân dân tệ yếu phản ánh kinh tế Trung Quốc yếu", "A weak yuan reflects a weak Chinese economy")],
                  [("Gói kích thích kinh tế Trung Quốc", "Chinese stimulus"), ("Quan hệ Mỹ - Trung", "US-China relations")]),
        "japan": ([("usdjpy", 1, "Yên yếu làm lợi nhuận xuất khẩu tăng", "A weak yen lifts export profits"),
                   ("nikkei", 1, "Đi cùng thị trường Nhật", "Moves with the Japanese market")],
                  [("Thuế quan ô tô của Mỹ", "US auto tariffs")]),
        "index_etf": ([("us10y", -1, "Lãi suất cao làm giảm định giá cổ phiếu nói chung", "Higher rates lower stock valuations"),
                       ("vix", -1, "Sợ hãi tăng thì cổ phiếu giảm", "Rising fear means falling stocks")],
                      [("Chính sách lãi suất của Fed", "Fed rate policy"), ("Lợi nhuận doanh nghiệp Mỹ", "US corporate earnings")]),
        "other": ([("sp500", 1, "Đi cùng thị trường chung", "Moves with the market")], []),
    },
    "crypto": {},
}
_CRYPTO_COMMON = [
    ("usd", -1, "USD mạnh lên hút tiền khỏi tài sản rủi ro như tiền số", "A strong dollar drains money from risk assets like crypto"),
    ("us10y", -1, "Lãi suất cao làm tiền số kém hấp dẫn so với trái phiếu", "Higher yields make crypto less attractive than bonds"),
    ("nasdaq", 1, "Tiền số thường đi cùng khẩu vị rủi ro của cổ phiếu công nghệ", "Crypto tends to follow risk appetite in tech stocks"),
]
_ALT = [("bitcoin", 1, "Altcoin gần như luôn đi theo Bitcoin, thường mạnh hơn cả chiều lên lẫn chiều xuống",
         "Altcoins almost always follow Bitcoin, usually more strongly both ways")] + _CRYPTO_COMMON
GROUPS["crypto"] = {
    "store_of_value": (_CRYPTO_COMMON + [("gold", 1, "Cùng được xem là tài sản chống mất giá tiền tệ", "Both are seen as hedges against currency debasement")],
                       [("Dòng tiền vào các quỹ ETF Bitcoin", "Flows into Bitcoin ETFs"), ("Chính sách lãi suất của Fed", "Fed rate policy"),
                        ("Lượng Bitcoin trên sàn và chu kỳ halving", "Bitcoin held on exchanges and the halving cycle")]),
    "smart_contract": (_ALT, [("Hoạt động mạng và tổng giá trị khóa (TVL)", "Network activity and total value locked"),
                              ("Lịch mở khóa token", "Token unlock schedules")]),
    "layer2": (_ALT, [("Hoạt động trên Ethereum và phí giao dịch", "Ethereum activity and fees"), ("Mở khóa token", "Token unlocks")]),
    "defi": (_ALT, [("Tổng giá trị khóa (TVL) và lợi suất", "Total value locked and yields"), ("Quy định với DeFi", "DeFi regulation")]),
    "infra": (_ALT, [("Số dự án dùng dịch vụ (oracle, lưu trữ)", "Projects using the service (oracles, storage)")]),
    "ai": (_ALT + [("nvidia", 1, "Câu chuyện AI trên cổ phiếu kéo theo nhóm coin AI", "The AI story in stocks carries AI coins")],
           [("Tin tức và sản phẩm AI", "AI news and products")]),
    "exchange": (_ALT, [("Khối lượng giao dịch của sàn", "Exchange trading volume"), ("Quy định với sàn giao dịch", "Exchange regulation")]),
    "meme": (_ALT, [("Tâm lý đám đông và mạng xã hội", "Crowd mood and social media"),
                    ("Rủi ro cực cao, có thể mất phần lớn giá trị rất nhanh", "Extremely risky, can lose most of its value fast")]),
    "payments": (_ALT, [("Quy định về thanh toán và các vụ kiện (XRP)", "Payment rules and lawsuits (XRP)")]),
    "privacy": (_ALT, [("Nguy cơ bị sàn hủy niêm yết", "Risk of exchange delistings")]),
    "rwa": (_ALT, [("Lợi suất trái phiếu Mỹ, tài sản thật được mã hóa", "US Treasury yields, tokenised real assets")]),
    "gold_token": ([("gold", 1, "Token đại diện cho vàng thật, giá đi theo giá vàng", "A token backed by physical gold, priced off gold"),
                    ("usd", -1, "USD mạnh thường làm giá vàng giảm", "A strong dollar usually weighs on gold")],
                   [("Lãi suất thực của Mỹ và nhu cầu trú ẩn", "US real rates and safe-haven demand")]),
    "other": (_ALT, []),
}

WORLD_GROUP = {
    **{s: "semis" for s in ("NVDA", "AMD", "AVGO", "TSM", "ASML", "005930.KS")},
    **{s: "bigtech" for s in ("AAPL", "MSFT", "GOOGL", "AMZN", "META", "NFLX", "PLTR")},
    **{s: "financials" for s in ("JPM", "V", "BRK-B")},
    "XOM": "energy", "COST": "consumer", "TSLA": "consumer", "LLY": "health", "COIN": "crypto_stock",
    "BABA": "china", "7203.T": "japan", "SPY": "index_etf", "QQQ": "index_etf",
}
COIN_GROUP = {
    "bitcoin": "store_of_value",
    **{c: "smart_contract" for c in ("ethereum", "solana", "cardano", "avalanche-2", "tron", "sui", "near", "aptos",
                                     "polkadot", "the-open-network", "internet-computer", "hedera-hashgraph", "cosmos",
                                     "algorand", "sei-network", "kaspa", "ethereum-classic", "tezos", "multiversx", "berachain-bera", "vechain")},
    **{c: "layer2" for c in ("arbitrum", "optimism", "polygon-ecosystem-token", "matic-network", "mantle", "starknet",
                             "immutable-x", "stacks", "blockstack")},
    **{c: "defi" for c in ("uniswap", "aave", "maker", "sky", "lido-dao", "jupiter-exchange-solana", "curve-dao-token",
                           "pendle", "ethena", "hyperliquid", "injective-protocol", "raydium", "thorchain", "jito-governance-token",
                           "aero", "aster-2", "ether-fi", "falcon-finance-ff", "just", "morpho", "nexo", "pancakeswap-token")},
    **{c: "infra" for c in ("chainlink", "the-graph", "filecoin", "arweave", "pyth-network", "quant-network", "celestia", "layerzero")},
    **{c: "ai" for c in ("render-token", "fetch-ai", "artificial-superintelligence-alliance", "bittensor", "virtual-protocol", "kite-2", "worldcoin-wld")},
    **{c: "exchange" for c in ("binancecoin", "okb", "crypto-com-chain", "leo-token", "bitget-token", "kucoin-shares",
                               "gatechain-token", "whitebit")},
    **{c: "meme" for c in ("dogecoin", "shiba-inu", "pepe", "bonk", "floki", "dogwifcoin", "official-trump", "brett",
                           "pudgy-penguins", "fartcoin", "spx6900", "pump-fun", "bianrensheng")},
    **{c: "payments" for c in ("ripple", "stellar", "litecoin", "bitcoin-cash", "xdce-crowd-sale", "world-liberty-financial", "dash")},
    **{c: "privacy" for c in ("monero", "zcash", "midnight-3")},
    **{c: "gold_token" for c in ("pax-gold", "tether-gold")},
    **{c: "rwa" for c in ("ondo-finance", "mantra-dao", "plume")},
}


def group_of(kind: str, r: dict) -> str:
    """The group a row belongs to: VN sector, world group, crypto category."""
    if kind == "vn":
        return r.get("sector") or "other"
    if kind == "world":
        return WORLD_GROUP.get(r.get("symbol") or "", "other")
    return COIN_GROUP.get(r.get("id") or "", "other")


def drivers_of(kind: str, group: str) -> list:
    g = GROUPS.get(kind, {})
    return (g.get(group) or g.get("other") or ([], []))[0]


# ---------------------------------------------------------------- series helpers

def _series(kind: str, symbol: str, raw_by_kind: dict) -> tuple[list, list]:
    b = (raw_by_kind.get(kind) or {}).get(symbol)
    if not b:
        return [], []
    return b["d"], b["c"]


def _trend_at(dates: list, closes: list, day: str) -> tuple[int, float | None]:
    """+1 / -1 / 0 for a driver on a day (its last close at or before it): above its 50-day average and up over the
    last 20 closes is +1, below and down is -1, anything mixed 0. Second value: the 20-close change in %."""
    i = bisect.bisect_right(dates, day) - 1
    if i < 50:
        return 0, None
    c = closes[i]
    sma50 = sum(closes[i - 49:i + 1]) / 50
    chg = (c / closes[i - 20] - 1) * 100 if closes[i - 20] else 0.0
    if c > sma50 and chg > 0:
        return 1, chg
    if c < sma50 and chg < 0:
        return -1, chg
    return 0, chg


def score_at(kind: str, group: str, day: str, raw_by_kind: dict) -> int | None:
    """The group's factor score on a past day, from expected signs and driver trends only (no look-ahead)."""
    vals = []
    for d, sign, _vi, _en in drivers_of(kind, group):
        dk, sym = DRIVERS[d]
        ds, cs = _series(dk, sym, raw_by_kind)
        if not ds:
            continue
        t, _ = _trend_at(ds, cs, day)
        vals.append(sign * t)
    return round(100 * sum(vals) / len(vals)) if vals else None


def _returns_on(dates_a, closes_a, dates_b, closes_b, last: int = 500, step: int = 5):
    """Returns of two series over the same windows of `step` common dates (weekly by default, not overlapping).
    Weekly, not daily: Vietnam closes before New York opens, so a US move shows up in Hanoi a session later and daily
    returns understate every world driver's effect."""
    ia = {d: i for i, d in enumerate(dates_a)}
    common = [d for d in dates_b if d in ia]
    common = common[-(last + 1):][::-1][::step][::-1]
    ib = {d: i for i, d in enumerate(dates_b)}
    ra, rb = [], []
    for d0, d1 in zip(common, common[1:]):
        a0, a1, b0, b1 = closes_a[ia[d0]], closes_a[ia[d1]], closes_b[ib[d0]], closes_b[ib[d1]]
        if a0 and b0:
            ra.append(a1 / a0 - 1)
            rb.append(b1 / b0 - 1)
    return ra, rb


def _corr_beta(x: list, y: list) -> tuple[float | None, float | None]:
    """Correlation of x with y, and beta of x on y."""
    n = len(x)
    if n < 20:
        return None, None
    mx, my = sum(x) / n, sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:
        return None, None
    return sxy / math.sqrt(sxx * syy), sxy / syy


def _group_index(members: list, raw: dict) -> tuple[list, list]:
    """Equal-weight index of the members' daily closes (level series starting at 100)."""
    dates = sorted({d for m in members for d in raw[m]["d"]})
    pos = {m: {d: i for i, d in enumerate(raw[m]["d"])} for m in members}
    level, out_d, out_c = 100.0, [], []
    prev = None
    for d in dates:
        if prev is not None:
            rets = []
            for m in members:
                i0, i1 = pos[m].get(prev), pos[m].get(d)
                if i0 is not None and i1 is not None and raw[m]["c"][i0]:
                    rets.append(raw[m]["c"][i1] / raw[m]["c"][i0] - 1)
            if rets:
                level *= 1 + sum(rets) / len(rets)
        out_d.append(d)
        out_c.append(level)
        prev = d
    return out_d, out_c


def _chg(dates, closes, n):
    if len(closes) <= n or not closes[-1 - n]:
        return None
    return (closes[-1] / closes[-1 - n] - 1) * 100


# ---------------------------------------------------------------- the analysis

_CACHE: dict = {}
_LOCK = threading.Lock()
TTL = 3600


def load_all() -> dict:
    import market_paper as mp
    return {k: mp.load_bars(k) for k in ("vn", "world", "crypto")}


def analyze(kind: str, raw_by_kind: dict | None = None, rows: list | None = None) -> dict:
    """Groups of one market with their drivers (expected, measured, now), factor score and relative strength."""
    import localefmt
    chu = localefmt.chu
    raw_by_kind = raw_by_kind if raw_by_kind is not None else load_all()
    raw = raw_by_kind.get(kind) or {}
    bench = {"vn": "VNINDEX", "world": "^GSPC", "crypto": "bitcoin"}[kind]
    meta = {(r.get("id") or r.get("symbol")): r for r in (rows or [])}
    members: dict[str, list] = {}
    for sym in raw:
        if sym == bench and kind != "crypto":
            continue
        r = meta.get(sym) or {"symbol": sym, "id": sym}
        if kind == "vn":
            import market_data as md
            if sym in dict(md.VN_INDICES):
                continue
            r = dict(r, sector=(md.VN_STOCKS.get(sym) or ("other", ""))[0])
        if kind == "world":
            if sym not in WORLD_GROUP:
                continue
        if kind == "crypto":
            r = dict(r, id=sym)
        if len(raw[sym]["c"]) < 60:
            continue
        members.setdefault(group_of(kind, r), []).append(sym)
    bd, bc = _series(kind, bench, raw_by_kind)
    today = max((raw[s]["d"][-1] for s in raw if raw[s]["d"]), default="")
    groups, symbols = [], {}
    for g, mems in members.items():
        gd, gc = _group_index(mems, raw)
        drivers, vals = [], []
        for d, sign, why_vi, why_en in drivers_of(kind, g):
            dk, dsym = DRIVERS[d]
            ds, cs = _series(dk, dsym, raw_by_kind)
            if not ds:
                drivers.append({"key": d, "name": chu(*DRIVER_NAMES[d]), "sign": sign, "why": chu(why_vi, why_en), "missing": True})
                continue
            t, chg1m = _trend_at(ds, cs, today)
            x, y = _returns_on(gd, gc, ds, cs)
            corr, beta = _corr_beta(x, y)
            vals.append(sign * t)
            drivers.append({
                "key": d, "name": chu(*DRIVER_NAMES[d]), "sign": sign, "why": chu(why_vi, why_en),
                "corr": None if corr is None else round(corr, 2), "beta": None if beta is None else round(beta, 2), "n": len(x),
                "confirmed": None if corr is None or abs(corr) < 0.1 else (corr > 0) == (sign > 0),
                "trend": t, "chg_1m": None if chg1m is None else round(chg1m, 2), "price": cs[-1], "effect": sign * t,
            })
        score = round(100 * sum(vals) / len(vals)) if vals else None
        n1, n3 = (30, 90) if kind == "crypto" else (21, 63)
        a1, a3 = _chg(gd, gc, n1), _chg(gd, gc, n3)
        rs1 = rs3 = None
        if bc and not (kind == "crypto" and g == "store_of_value"):   # Bitcoin against itself says nothing
            b1, b3 = _chg(bd, bc, n1), _chg(bd, bc, n3)
            rs1 = None if a1 is None or b1 is None else round(a1 - b1, 2)
            rs3 = None if a3 is None or b3 is None else round(a3 - b3, 2)
        qual = [chu(vi, en) for vi, en in (GROUPS[kind].get(g) or GROUPS[kind]["other"])[1]]
        drivers.sort(key=lambda x: -abs(x.get("corr") or 0))
        groups.append({"key": g, "count": len(mems), "members": sorted(mems), "score": score, "rs_1m": rs1, "rs_3m": rs3,
                       "chg_1m": None if a1 is None else round(a1, 2), "drivers": drivers, "watch": qual})
        for m in mems:
            symbols[m] = {"group": g, "factor": score}
    groups.sort(key=lambda x: -((x["score"] or 0) + (x["rs_1m"] or 0)))
    return {"ok": True, "kind": kind, "at": time.time(), "day": today, "groups": groups, "symbols": symbols}


def cached(kind: str, rows: list | None = None, max_age: float = TTL) -> dict:
    """`analyze` at most once an hour per market (it reads the whole archive)."""
    with _LOCK:
        hit = _CACHE.get(kind)
        if hit and time.time() - hit["at"] < max_age:
            return hit
    res = analyze(kind, rows=rows)
    with _LOCK:
        _CACHE[kind] = res
    return res
