"""레거시 데이터 로더 (M6): 매핑 명세로 레거시 내보내기(CSV)를 가져와 데이터셋 스냅샷을 만들고,
시나리오 세트의 기간 케이스와 레거시 기준선(과거 실제 배정)에 쓴다."""

from .dataset import data_dir, dataset_instance, legacy_decisions, list_datasets, load_snapshot
from .export import export_legacy
from .importer import ImportRefused, import_legacy
from .mapping import load_mapping, mapping_errors

__all__ = ["ImportRefused", "data_dir", "dataset_instance", "export_legacy", "import_legacy", "legacy_decisions",
           "list_datasets", "load_mapping", "load_snapshot", "mapping_errors"]
