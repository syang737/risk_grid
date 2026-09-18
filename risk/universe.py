"""A real instrument universe for the synthetic book.

Tickers, sector and industry are facts, hard-coded here so the generator needs
no network and no data dependency. `price` is different, and the distinction
matters: **it is an indicative level for demo and load-test data, not a quote.**
Nothing in this repository is market data, and nothing here should be used as
if it were. It exists so a screenshot shows AAPL in the two hundreds rather
than at $12, which the old lognormal draw was free to do.

Sector and industry labels are plain English at roughly the granularity the
incumbent's Product column shows. They are deliberately not branded as GICS,
which is a licensed taxonomy.

Volatility is generated rather than tabulated -- a per-sector base with a few
per-industry overrides, jittered per name in `synthetic.py`. A per-symbol vol
number would be a much stronger claim than a price band, with nothing behind it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Symbol:
    ticker: str
    sector: str
    industry: str
    price: float  # indicative level; see the module docstring


# Annualised vol by sector, with overrides for industries that plainly do not
# behave like their sector average.
SECTOR_VOL: dict[str, float] = {
    "Technology": 0.34,
    "Financials": 0.26,
    "Health Care": 0.28,
    "Consumer Discretionary": 0.30,
    "Consumer Staples": 0.18,
    "Industrials": 0.25,
    "Energy": 0.33,
    "Utilities": 0.18,
    "Real Estate": 0.24,
    "Materials": 0.27,
    "Communication Services": 0.30,
    "Funds & ETFs": 0.17,
}

INDUSTRY_VOL: dict[str, float] = {
    "Biotechnology": 0.55,
    "Semiconductors": 0.45,
    "Autos": 0.42,
    "Airlines": 0.38,
    "Coal & Uranium": 0.50,
    "Oil Services & Equipment": 0.40,
    "Independent Power": 0.38,
    "Interactive Media": 0.38,
    "Volatility & Leveraged": 0.90,
    "Broad Market Index": 0.15,
    "Fixed Income ETFs": 0.10,
    "Commodity & Currency ETFs": 0.20,
}


# sector -> industry -> ((ticker, indicative price), ...)
_TABLE: dict[str, dict[str, tuple[tuple[str, float], ...]]] = {
    "Technology": {
        "Semiconductors": (
            ("NVDA", 175.0), ("AMD", 160.0), ("INTC", 25.0), ("AVGO", 180.0),
            ("QCOM", 165.0), ("TXN", 200.0), ("MU", 110.0), ("ADI", 235.0),
            ("NXPI", 225.0), ("MRVL", 90.0), ("ON", 60.0), ("MCHP", 60.0),
            ("SWKS", 75.0), ("QRVO", 80.0), ("MPWR", 800.0), ("TER", 130.0),
            ("LRCX", 95.0), ("AMAT", 175.0), ("KLAC", 850.0), ("ASML", 750.0),
            ("TSM", 200.0), ("GFS", 40.0), ("ARM", 140.0), ("SMCI", 40.0),
        ),
        "Software - Infrastructure": (
            ("MSFT", 425.0), ("ORCL", 175.0), ("CRM", 270.0), ("ADBE", 380.0),
            ("NOW", 950.0), ("PANW", 190.0), ("CRWD", 350.0), ("ZS", 200.0),
            ("OKTA", 95.0), ("DDOG", 130.0), ("SNOW", 165.0), ("MDB", 250.0),
            ("NET", 100.0), ("FTNT", 95.0), ("TEAM", 220.0), ("CHKP", 175.0),
            ("GEN", 27.0), ("AKAM", 90.0), ("DT", 50.0), ("ESTC", 90.0),
        ),
        "Software - Application": (
            ("INTU", 640.0), ("ADSK", 300.0), ("WDAY", 240.0), ("HUBS", 550.0),
            ("VEEV", 230.0), ("ANSS", 340.0), ("CDNS", 300.0), ("SNPS", 500.0),
            ("PTC", 180.0), ("SSNC", 75.0), ("TYL", 570.0), ("PAYC", 190.0),
            ("PCTY", 180.0), ("ZM", 75.0), ("DOCU", 60.0), ("TWLO", 105.0),
            ("U", 25.0), ("RBLX", 45.0),
        ),
        "Hardware & Equipment": (
            ("AAPL", 230.0), ("DELL", 120.0), ("HPQ", 35.0), ("HPE", 20.0),
            ("NTAP", 120.0), ("WDC", 65.0), ("STX", 105.0), ("ANET", 380.0),
            ("CSCO", 55.0), ("MSI", 450.0), ("JNPR", 37.0), ("ZBRA", 320.0),
            ("KEYS", 160.0), ("TDY", 450.0), ("GLW", 45.0), ("APH", 65.0),
            ("TEL", 150.0), ("JBL", 130.0), ("FLEX", 35.0),
        ),
        "IT Services": (
            ("IBM", 230.0), ("ACN", 340.0), ("INFY", 20.0), ("CTSH", 78.0),
            ("EPAM", 200.0), ("GDDY", 155.0), ("VRSN", 190.0), ("IT", 480.0),
            ("CDW", 220.0), ("DXC", 20.0),
        ),
        "Payments & Fintech": (
            ("V", 285.0), ("MA", 470.0), ("PYPL", 70.0), ("FIS", 80.0),
            ("FI", 200.0), ("GPN", 105.0), ("ADP", 280.0), ("PAYX", 130.0),
            ("TOST", 35.0), ("AFRM", 45.0), ("XYZ", 70.0),
        ),
    },
    "Financials": {
        "Banks - Money Center": (
            ("JPM", 215.0), ("BAC", 40.0), ("C", 65.0), ("WFC", 58.0),
        ),
        "Banks - Regional": (
            ("USB", 45.0), ("PNC", 185.0), ("TFC", 42.0), ("FITB", 42.0),
            ("KEY", 17.0), ("RF", 22.0), ("CFG", 40.0), ("HBAN", 14.0),
            ("MTB", 175.0), ("ZION", 48.0), ("CMA", 55.0), ("WAL", 80.0),
            ("EWBC", 90.0), ("SNV", 45.0), ("PB", 75.0), ("FHN", 16.0),
        ),
        "Investment Banking & Brokerage": (
            ("GS", 500.0), ("MS", 105.0), ("SCHW", 70.0), ("RJF", 120.0),
            ("LPLA", 250.0), ("JEF", 60.0), ("EVR", 250.0), ("LAZ", 50.0),
            ("PJT", 150.0), ("HLI", 165.0),
        ),
        "Asset Management": (
            ("BLK", 950.0), ("BX", 145.0), ("KKR", 125.0), ("APO", 120.0),
            ("ARES", 145.0), ("TROW", 110.0), ("BEN", 20.0), ("IVZ", 17.0),
            ("AMP", 450.0), ("NTRS", 90.0), ("STT", 88.0), ("BK", 70.0),
        ),
        "Insurance - Property & Casualty": (
            ("PGR", 240.0), ("TRV", 245.0), ("ALL", 190.0), ("CB", 280.0),
            ("AIG", 75.0), ("HIG", 115.0), ("CINF", 140.0), ("WRB", 60.0),
            ("MKL", 1550.0),
        ),
        "Insurance - Life & Health": (
            ("MET", 80.0), ("PRU", 115.0), ("AFL", 105.0), ("UNM", 60.0),
            ("GL", 110.0), ("PFG", 80.0), ("LNC", 30.0),
        ),
        "Insurance Brokers": (
            ("MMC", 220.0), ("AON", 350.0), ("AJG", 280.0), ("BRO", 105.0),
            ("WTW", 310.0),
        ),
        "Exchanges & Market Data": (
            ("SPGI", 500.0), ("MCO", 470.0), ("CME", 220.0), ("ICE", 155.0),
            ("NDAQ", 72.0), ("CBOE", 200.0), ("MSCI", 570.0), ("MKTX", 230.0),
            ("FDS", 460.0), ("TW", 130.0),
        ),
        "Consumer Finance": (
            ("AXP", 260.0), ("COF", 145.0), ("DFS", 130.0), ("SYF", 50.0),
            ("ALLY", 38.0), ("SOFI", 8.0), ("NAVI", 15.0),
        ),
        "Diversified Holdings": (
            ("BRK.B", 450.0),
        ),
    },
    "Health Care": {
        "Pharmaceuticals": (
            ("JNJ", 160.0), ("PFE", 28.0), ("MRK", 110.0), ("LLY", 900.0),
            ("ABBV", 190.0), ("BMY", 50.0), ("ZTS", 180.0), ("VTRS", 11.0),
            ("JAZZ", 115.0), ("PRGO", 27.0),
        ),
        "Biotechnology": (
            ("AMGN", 320.0), ("GILD", 90.0), ("BIIB", 200.0), ("VRTX", 470.0),
            ("REGN", 950.0), ("MRNA", 60.0), ("BNTX", 105.0), ("ALNY", 250.0),
            ("INCY", 65.0), ("EXAS", 60.0), ("IONS", 40.0), ("SRPT", 120.0),
            ("NBIX", 130.0), ("UTHR", 350.0), ("BMRN", 75.0), ("HALO", 55.0),
        ),
        "Medical Devices": (
            ("ABT", 110.0), ("MDT", 85.0), ("SYK", 350.0), ("BSX", 85.0),
            ("BDX", 235.0), ("EW", 70.0), ("ISRG", 480.0), ("ZBH", 110.0),
            ("BAX", 35.0), ("RMD", 230.0), ("DXCM", 70.0), ("PODD", 240.0),
            ("ALGN", 220.0), ("TFX", 220.0), ("XRAY", 20.0), ("HOLX", 80.0),
        ),
        "Health Care Providers": (
            ("UNH", 560.0), ("ELV", 500.0), ("CI", 340.0), ("CVS", 60.0),
            ("HUM", 300.0), ("CNC", 70.0), ("MOH", 300.0), ("HCA", 350.0),
            ("UHS", 220.0), ("THC", 150.0), ("DVA", 155.0), ("EHC", 95.0),
        ),
        "Life Sciences Tools": (
            ("TMO", 590.0), ("DHR", 250.0), ("A", 140.0), ("MTD", 1300.0),
            ("WAT", 350.0), ("IQV", 210.0), ("CRL", 200.0), ("ILMN", 130.0),
            ("RVTY", 110.0), ("WST", 320.0), ("AVTR", 22.0),
        ),
        "Health Care Distribution": (
            ("MCK", 620.0), ("COR", 260.0), ("CAH", 110.0), ("HSIC", 70.0),
            ("OMI", 15.0),
        ),
    },
    "Consumer Discretionary": {
        "Internet Retail": (
            ("AMZN", 200.0), ("EBAY", 60.0), ("ETSY", 55.0), ("CHWY", 30.0),
            ("W", 55.0), ("CVNA", 200.0),
        ),
        "Specialty Retail": (
            ("HD", 400.0), ("LOW", 250.0), ("TJX", 115.0), ("ROST", 145.0),
            ("BURL", 260.0), ("ORLY", 1150.0), ("AZO", 3000.0), ("BBY", 90.0),
            ("ULTA", 390.0), ("DKS", 220.0), ("TSCO", 275.0), ("FIVE", 100.0),
            ("GPS", 25.0), ("AEO", 18.0), ("ANF", 130.0), ("WSM", 130.0),
        ),
        "Restaurants": (
            ("MCD", 300.0), ("SBUX", 95.0), ("CMG", 55.0), ("YUM", 135.0),
            ("QSR", 68.0), ("DRI", 165.0), ("DPZ", 430.0), ("WEN", 17.0),
            ("TXRH", 170.0), ("WING", 300.0), ("SHAK", 110.0), ("CAVA", 90.0),
        ),
        "Hotels, Travel & Leisure": (
            ("MAR", 265.0), ("HLT", 235.0), ("H", 145.0), ("BKNG", 4500.0),
            ("ABNB", 130.0), ("EXPE", 170.0), ("RCL", 230.0), ("CCL", 25.0),
            ("NCLH", 22.0), ("LVS", 45.0), ("MGM", 38.0), ("WYNN", 90.0),
            ("CZR", 38.0), ("DKNG", 40.0),
        ),
        "Autos": (
            ("TSLA", 340.0), ("GM", 50.0), ("F", 11.0), ("RIVN", 14.0),
            ("LCID", 3.0), ("APTV", 70.0), ("BWA", 35.0), ("LEA", 105.0),
            ("MGA", 45.0), ("HMC", 30.0), ("TM", 185.0),
        ),
        "Household Durables": (
            ("NVR", 8000.0), ("DHI", 165.0), ("LEN", 150.0), ("PHM", 125.0),
            ("TOL", 135.0), ("MAS", 70.0), ("MHK", 130.0), ("WHR", 105.0),
            ("LEG", 11.0),
        ),
        "Apparel & Luxury": (
            ("NKE", 75.0), ("DECK", 110.0), ("CROX", 100.0), ("SKX", 65.0),
            ("RL", 230.0), ("PVH", 80.0), ("TPR", 65.0), ("CPRI", 20.0),
            ("UAA", 7.0), ("VFC", 20.0), ("LULU", 300.0),
        ),
    },
    "Consumer Staples": {
        "Beverages": (
            ("KO", 70.0), ("PEP", 145.0), ("MNST", 55.0), ("KDP", 35.0),
            ("STZ", 190.0), ("BF.B", 35.0), ("TAP", 55.0), ("CELH", 40.0),
        ),
        "Food Products": (
            ("MDLZ", 65.0), ("GIS", 60.0), ("K", 80.0), ("KHC", 30.0),
            ("HSY", 175.0), ("CAG", 25.0), ("CPB", 42.0), ("SJM", 110.0),
            ("HRL", 30.0), ("TSN", 60.0), ("MKC", 75.0), ("LW", 60.0),
            ("INGR", 130.0), ("ADM", 55.0), ("BG", 80.0),
        ),
        "Household & Personal Products": (
            ("PG", 165.0), ("CL", 90.0), ("KMB", 130.0), ("CHD", 105.0),
            ("CLX", 145.0), ("EL", 75.0), ("COTY", 6.0), ("ELF", 80.0),
        ),
        "Food & Staples Retailing": (
            ("WMT", 95.0), ("COST", 900.0), ("TGT", 100.0), ("KR", 65.0),
            ("DG", 90.0), ("DLTR", 70.0), ("SYY", 75.0), ("USFD", 65.0),
            ("CASY", 400.0),
        ),
        "Tobacco": (
            ("PM", 165.0), ("MO", 55.0),
        ),
    },
    "Industrials": {
        "Aerospace & Defense": (
            ("BA", 180.0), ("LMT", 450.0), ("RTX", 130.0), ("NOC", 480.0),
            ("GD", 280.0), ("LHX", 250.0), ("TDG", 1400.0), ("HWM", 120.0),
            ("HEI", 270.0), ("TXT", 75.0), ("SPR", 35.0), ("AXON", 700.0),
            ("LDOS", 170.0),
        ),
        "Machinery": (
            ("CAT", 400.0), ("DE", 480.0), ("CMI", 350.0), ("PCAR", 100.0),
            ("ITW", 250.0), ("PH", 700.0), ("EMR", 120.0), ("ETN", 330.0),
            ("ROK", 300.0), ("DOV", 190.0), ("IR", 90.0), ("XYL", 125.0),
            ("FTV", 70.0), ("AME", 180.0), ("GGG", 85.0), ("NDSN", 210.0),
            ("SWK", 85.0),
        ),
        "Airlines": (
            ("DAL", 60.0), ("UAL", 95.0), ("AAL", 13.0), ("LUV", 30.0),
            ("ALK", 60.0), ("JBLU", 6.0),
        ),
        "Road & Rail": (
            ("UNP", 230.0), ("CSX", 33.0), ("NSC", 260.0), ("ODFL", 180.0),
            ("JBHT", 155.0), ("CHRW", 100.0), ("XPO", 130.0), ("SAIA", 400.0),
            ("LSTR", 160.0),
        ),
        "Air Freight & Logistics": (
            ("UPS", 105.0), ("FDX", 250.0), ("EXPD", 120.0), ("GXO", 50.0),
        ),
        "Commercial Services": (
            ("WM", 215.0), ("RSG", 240.0), ("WCN", 185.0), ("CTAS", 210.0),
            ("FAST", 80.0), ("GWW", 1000.0), ("URI", 850.0), ("CPRT", 50.0),
            ("VRSK", 275.0), ("EFX", 260.0), ("BR", 240.0), ("ROL", 50.0),
        ),
        "Electrical Equipment & Conglomerates": (
            ("GE", 195.0), ("HON", 215.0), ("MMM", 140.0), ("ABB", 55.0),
            ("GNRC", 165.0), ("ACM", 115.0), ("J", 135.0), ("PWR", 320.0),
            ("EME", 480.0), ("FIX", 450.0),
        ),
        "Building Products": (
            ("JCI", 80.0), ("CARR", 75.0), ("TT", 400.0), ("LII", 600.0),
            ("AOS", 75.0), ("ALLE", 145.0), ("BLDR", 130.0),
        ),
    },
    "Energy": {
        "Integrated Oil & Gas": (
            ("XOM", 115.0), ("CVX", 155.0),
        ),
        "Exploration & Production": (
            ("COP", 105.0), ("EOG", 125.0), ("DVN", 35.0), ("FANG", 160.0),
            ("OXY", 45.0), ("HES", 145.0), ("APA", 22.0), ("CTRA", 25.0),
            ("MTDR", 50.0), ("AR", 30.0), ("RRC", 35.0), ("EQT", 50.0),
            ("CHRD", 105.0), ("PR", 13.0),
        ),
        "Oil Services & Equipment": (
            ("SLB", 40.0), ("HAL", 25.0), ("BKR", 40.0), ("NOV", 14.0),
            ("FTI", 30.0), ("CHX", 35.0), ("WFRD", 60.0), ("TDW", 45.0),
            ("RIG", 3.0),
        ),
        "Refining & Marketing": (
            ("MPC", 150.0), ("VLO", 130.0), ("PSX", 125.0), ("DINO", 40.0),
            ("DK", 17.0), ("PARR", 18.0),
        ),
        "Midstream": (
            ("KMI", 25.0), ("WMB", 55.0), ("OKE", 90.0), ("LNG", 200.0),
            ("TRGP", 165.0), ("EPD", 32.0), ("ET", 18.0), ("MPLX", 50.0),
        ),
        "Coal & Uranium": (
            ("BTU", 20.0), ("ARCH", 130.0), ("CEIX", 100.0), ("CCJ", 60.0),
            ("UEC", 7.0),
        ),
    },
    "Utilities": {
        "Electric Utilities": (
            ("NEE", 75.0), ("DUK", 115.0), ("SO", 90.0), ("D", 55.0),
            ("AEP", 100.0), ("EXC", 40.0), ("XEL", 70.0), ("ED", 100.0),
            ("WEC", 100.0), ("ES", 60.0), ("FE", 42.0), ("PPL", 34.0),
            ("PNW", 90.0), ("IDA", 105.0), ("POR", 47.0),
        ),
        "Multi-Utilities": (
            ("PCG", 18.0), ("SRE", 85.0), ("DTE", 130.0), ("AEE", 95.0),
            ("CMS", 70.0), ("NI", 38.0), ("CNP", 35.0), ("LNT", 62.0),
            ("EVRG", 65.0), ("OGE", 43.0),
        ),
        "Independent Power": (
            ("VST", 180.0), ("NRG", 110.0), ("CEG", 280.0), ("TLN", 220.0),
        ),
        "Gas & Water Utilities": (
            ("ATO", 145.0), ("AWK", 135.0), ("WTRG", 38.0), ("SJW", 55.0),
            ("CWT", 50.0), ("NJR", 47.0), ("SWX", 75.0),
        ),
    },
    "Real Estate": {
        "REITs - Industrial & Storage": (
            ("PLD", 110.0), ("EXR", 145.0), ("PSA", 300.0), ("CUBE", 45.0),
            ("FR", 55.0), ("EGP", 175.0), ("STAG", 37.0),
        ),
        "REITs - Residential": (
            ("AVB", 220.0), ("EQR", 70.0), ("MAA", 155.0), ("ESS", 290.0),
            ("UDR", 42.0), ("CPT", 120.0), ("INVH", 33.0), ("AMH", 36.0),
            ("ELS", 65.0), ("SUI", 130.0),
        ),
        "REITs - Retail": (
            ("SPG", 175.0), ("O", 60.0), ("REG", 75.0), ("FRT", 110.0),
            ("KIM", 24.0), ("BRX", 28.0), ("NNN", 42.0), ("ADC", 75.0),
        ),
        "REITs - Office & Diversified": (
            ("BXP", 75.0), ("VNO", 40.0), ("SLG", 70.0), ("HIW", 32.0),
            ("DEI", 18.0), ("WPC", 60.0),
        ),
        "REITs - Infrastructure & Specialty": (
            ("AMT", 200.0), ("CCI", 100.0), ("SBAC", 230.0), ("EQIX", 850.0),
            ("DLR", 170.0), ("IRM", 110.0), ("WY", 30.0), ("PCH", 40.0),
            ("RYN", 28.0), ("LAMR", 125.0), ("VICI", 32.0), ("GLPI", 50.0),
        ),
        "REITs - Health Care": (
            ("WELL", 145.0), ("VTR", 65.0), ("OHI", 38.0), ("DOC", 20.0),
            ("CTRE", 30.0), ("SBRA", 18.0),
        ),
        "Real Estate Services": (
            ("CBRE", 130.0), ("JLL", 260.0), ("CSGP", 75.0), ("ZG", 70.0),
            ("COMP", 7.0),
        ),
    },
    "Materials": {
        "Chemicals": (
            ("LIN", 460.0), ("SHW", 350.0), ("APD", 300.0), ("ECL", 250.0),
            ("DD", 80.0), ("DOW", 40.0), ("PPG", 115.0), ("LYB", 75.0),
            ("ALB", 85.0), ("CE", 60.0), ("EMN", 90.0), ("FMC", 45.0),
            ("IFF", 80.0), ("RPM", 120.0), ("AXTA", 35.0), ("CBT", 100.0),
            ("HUN", 18.0), ("OLN", 25.0), ("WLK", 110.0),
        ),
        "Metals & Mining": (
            ("NEM", 55.0), ("FCX", 45.0), ("NUE", 125.0), ("STLD", 130.0),
            ("CLF", 10.0), ("X", 40.0), ("AA", 35.0), ("RS", 290.0),
            ("CMC", 50.0), ("ATI", 70.0), ("MP", 50.0), ("HL", 7.0),
            ("AEM", 110.0), ("GOLD", 20.0), ("RGLD", 155.0), ("PAAS", 25.0),
            ("WPM", 65.0),
        ),
        "Construction Materials": (
            ("VMC", 275.0), ("MLM", 550.0), ("EXP", 250.0), ("SUM", 50.0),
            ("CX", 6.0),
        ),
        "Packaging & Containers": (
            ("BALL", 55.0), ("CCK", 95.0), ("IP", 50.0), ("PKG", 220.0),
            ("AMCR", 10.0), ("SEE", 33.0), ("SON", 50.0), ("AVY", 200.0),
            ("GPK", 25.0), ("BERY", 65.0),
        ),
    },
    "Communication Services": {
        "Interactive Media": (
            ("GOOGL", 190.0), ("META", 600.0), ("PINS", 32.0), ("SNAP", 11.0),
            ("RDDT", 100.0), ("MTCH", 35.0), ("BMBL", 6.0), ("YELP", 35.0),
            ("TRIP", 15.0),
        ),
        "Entertainment": (
            ("DIS", 95.0), ("NFLX", 900.0), ("WBD", 10.0), ("PARA", 11.0),
            ("LYV", 130.0), ("SPOT", 480.0), ("EA", 155.0), ("TTWO", 190.0),
            ("WMG", 30.0), ("MSGE", 40.0), ("FWONK", 90.0),
        ),
        "Telecom": (
            ("T", 22.0), ("VZ", 42.0), ("TMUS", 240.0), ("LUMN", 7.0),
            ("FYBR", 35.0), ("USM", 60.0), ("IRDM", 28.0), ("VSAT", 12.0),
        ),
        "Cable & Satellite": (
            ("CMCSA", 40.0), ("CHTR", 350.0), ("SIRI", 22.0), ("LBRDK", 80.0),
            ("CABO", 200.0),
        ),
        "Advertising & Publishing": (
            ("OMC", 90.0), ("IPG", 28.0), ("NYT", 55.0), ("NWSA", 28.0),
            ("TTD", 110.0), ("CRTO", 40.0), ("MGNI", 15.0), ("PUBM", 12.0),
        ),
    },
    "Funds & ETFs": {
        "Broad Market Index": (
            ("SPY", 580.0), ("VOO", 530.0), ("IVV", 580.0), ("VTI", 290.0),
            ("QQQ", 500.0), ("DIA", 430.0), ("IWM", 225.0), ("MDY", 570.0),
            ("RSP", 175.0),
        ),
        "Sector ETFs": (
            ("XLF", 48.0), ("XLE", 90.0), ("XLK", 235.0), ("XLV", 145.0),
            ("XLI", 135.0), ("XLY", 210.0), ("XLP", 82.0), ("XLU", 80.0),
            ("XLB", 92.0), ("XLRE", 43.0), ("XLC", 100.0), ("SMH", 250.0),
            ("XBI", 95.0), ("KRE", 60.0), ("ITB", 115.0), ("OIH", 300.0),
        ),
        "Fixed Income ETFs": (
            ("TLT", 90.0), ("IEF", 95.0), ("SHY", 82.0), ("AGG", 100.0),
            ("LQD", 110.0), ("HYG", 80.0), ("JNK", 98.0), ("TIP", 108.0),
            ("BND", 74.0),
        ),
        "Commodity & Currency ETFs": (
            ("GLD", 250.0), ("SLV", 28.0), ("USO", 75.0), ("UNG", 15.0),
            ("DBC", 22.0), ("UUP", 29.0),
        ),
        "International ETFs": (
            ("EFA", 82.0), ("EEM", 44.0), ("FXI", 30.0), ("EWJ", 70.0),
            ("EWZ", 27.0), ("VGK", 68.0), ("INDA", 55.0),
        ),
        "Volatility & Leveraged": (
            ("VXX", 50.0), ("UVXY", 20.0), ("TQQQ", 75.0), ("SQQQ", 30.0),
            ("SOXL", 30.0), ("TNA", 40.0),
        ),
    },
}


def _build() -> tuple[Symbol, ...]:
    out: list[Symbol] = []
    seen: set[str] = set()
    for sector, industries in _TABLE.items():
        if sector not in SECTOR_VOL:
            raise ValueError(f"no vol for sector {sector!r}")
        for industry, members in industries.items():
            for ticker, price in members:
                if ticker in seen:
                    raise ValueError(f"duplicate ticker {ticker!r}")
                seen.add(ticker)
                out.append(Symbol(ticker, sector, industry, price))
    return tuple(out)


UNIVERSE: tuple[Symbol, ...] = _build()
BY_TICKER: dict[str, Symbol] = {s.ticker: s for s in UNIVERSE}

SECTORS: tuple[str, ...] = tuple(_TABLE)
INDUSTRIES: tuple[str, ...] = tuple(
    industry for industries in _TABLE.values() for industry in industries
)


def annual_vol(symbol: Symbol) -> float:
    """Indicative annualised vol: the industry's, else its sector's."""
    return INDUSTRY_VOL.get(symbol.industry, SECTOR_VOL[symbol.sector])


def sample(n: int, rng: np.random.Generator) -> tuple[Symbol, ...]:
    """`n` distinct symbols, or the whole universe if it is smaller.

    The cap is announced rather than applied quietly: a load test asking for
    5,000 underlyings against a 700-name table would otherwise get a book with
    a silently different cardinality, which is the one property these
    generated books exist to control.
    """
    if n >= len(UNIVERSE):
        if n > len(UNIVERSE):
            import warnings

            warnings.warn(
                f"universe holds {len(UNIVERSE)} symbols; using all of them "
                f"rather than the {n} requested",
                stacklevel=2,
            )
        return UNIVERSE
    picks = rng.choice(len(UNIVERSE), size=n, replace=False)
    return tuple(UNIVERSE[i] for i in sorted(picks))
