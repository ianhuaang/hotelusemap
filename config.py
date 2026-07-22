"""Pipeline configuration constants."""

from pathlib import Path

# Paths
PROJECT_ROOT = Path(__file__).parent
DATA_RAW = PROJECT_ROOT / "data" / "raw"
DATA_PROCESSED = PROJECT_ROOT / "data" / "processed"
CACHE_DIR = PROJECT_ROOT / "cache"

# Socrata dataset IDs
PLUTO_DATASET_ID = "64uk-42ks"
BUILDING_FOOTPRINTS_DATASET_ID = "5zhs-2jue"
HPD_BUILDINGS_DATASET_ID = "kj4p-ruqc"
DOF_SALES_DATASET_ID = "w2pb-icbu"
DOB_FILINGS_DATASET_ID = "ic3t-wcy2"
COO_LEGACY_DATASET_ID = "bs8b-p36w"   # DOB BIS C of O (2012–Mar 2021)
COO_NOW_DATASET_ID = "pkdm-hqz6"      # DOB NOW C of O (Mar 2021+)
HPD_VIOLATIONS_DATASET_ID = "wvxf-dwi5"
DOB_ECB_VIOLATIONS_DATASET_ID = "6bgk-3dad"
TAX_LIENS_DATASET_ID = "9rz4-mjek"
ACRIS_LEGALS_DATASET_ID = "8h5j-fqxa"
ACRIS_MASTER_DATASET_ID = "bnx9-e6tj"

SOCRATA_BASE_URL = "https://data.cityofnewyork.us/resource"

# GeoSearch
GEOSEARCH_URL = "https://geosearch.planninglabs.nyc/v2/search"

# Pipeline thresholds — deliberately low; tighten later
MIN_RESIDENTIAL_UNITS = 20

# Target community districts outside Manhattan (borough digit + 2-digit CD)
# Manhattan: all CDs included (no CD filter)
# Brooklyn: 301=Williamsburg/Greenpoint, 302=Downtown BK/DUMBO/Heights, 306=Park Slope/Gowanus
# Queens: 401=LIC/Astoria
TARGET_CDS_BK = {"301", "302", "306"}
TARGET_CDS_QN = {"401"}
TARGET_CDS = TARGET_CDS_BK | TARGET_CDS_QN

# Borough codes (PLUTO uses text, HPD uses numeric)
BOROUGH_TEXT_TO_NUM = {"MN": "1", "BX": "2", "BK": "3", "QN": "4", "SI": "5"}
BOROUGH_NUM_TO_TEXT = {v: k for k, v in BOROUGH_TEXT_TO_NUM.items()}

# Tiers
TIER_LEGAL_TRANSIENT = "legal_transient"
TIER_CLASS_B = "class_b"
TIER_PARTIAL = "partial"
TIER_UNKNOWN = "unknown"
TIER_EXCLUDED = "excluded"
TIER_PRIOR_OPERATOR = "prior_operator"

# Confidence
CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
