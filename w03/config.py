import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 원본 데이터룸 — 읽기 전용. 앱은 이 파일들을 SQLite로 복사해서만 쓴다.
SOURCE_DIR = ROOT / "01_참가자_배포용" / "04_원본데이터"
# 실행 상태 DB는 저장소 밖(임시 폴더)에 둔다. W03_DB 로 바꿀 수 있다.
DB_PATH = Path(os.environ.get("W03_DB", Path(tempfile.gettempdir()) / "axwar_w03.db"))

AS_OF = "2026-07-01"
RECALL_THRESHOLD_PCT = 10.0          # quality_policy · calculation_definitions 1.9
RESUPPLY_LINE = "L-02"               # PL-02 더마 전용 라인 (rfp_c201, m_product_cv)
RESUPPLY_MATERIAL = "M-B"            # PL-02 주원료 (m_product_cv.main_material_id)

CLAUDE_MODEL = "claude-opus-5-5"
# AI_PROVIDER=gemini 일 때 쓰는 모델 — Gemini API 무료 등급(Standard)이 있는 Flash 계열. GEMINI_MODEL 로 바꿀 수 있다.
# 기본은 가장 가벼운 무료 Flash-Lite: 2026-11-11 실호출에서 gemini-3.8-flash·3.6-flash는 503(과부하)이 반복됐다.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

# 원본 CSV → SQLite 테이블
SOURCE_TABLES = {
    "src_claims": "01_corevia/quality/qc_claims_cv.csv",
    "src_batches": "01_corevia/production/prod_batch_log_cv.csv",
    "src_sbl": "01_corevia/quality/shipment_batch_link_cv.csv",
    "src_lots": "01_corevia/procurement/material_lot_master_cv.csv",
    "src_purchases": "01_corevia/procurement/procurement_material_purchase_cv.csv",
    "src_consumption": "01_corevia/production/production_material_consumption_cv.csv",
    "src_capacity": "01_corevia/production/strat_capacity_cv.csv",
    "src_customers": "01_corevia/master/m_customer_cv.csv",
    "src_materials": "01_corevia/master/m_material_cv.csv",
    "src_products": "01_corevia/master/m_product_cv.csv",
    "src_capex": "01_corevia/production/strat_capex_plan_cv.csv",
    "src_orders": "01_corevia/sales/sales_orders_cv.csv",
    "src_sales_shipments": "01_corevia/sales/sales_shipments_cv.csv",
}
