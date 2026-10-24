"""The additive delivery column must not weaken historical-generation guards."""
import importlib.util
from pathlib import Path


def snapshot_row():
    source=Path(__file__).resolve().parents[1]/'scripts/deploy_pre.py'
    spec=importlib.util.spec_from_file_location('too_deploy_snapshot',source)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    namespace={}
    exec(module.TASK_ROW_SNAPSHOT,namespace)
    return namespace['task_snapshot_row']


def test_empty_delivery_column_preserves_existing_task_snapshot():
    normalize=snapshot_row()
    old={'id':'too_existing','status':'succeeded','result_json':'["old-url"]','actual_cost':3}
    assert normalize({**old,'result_transfer_json':'{}'})==normalize(old)
    assert normalize({**old,'result_transfer_json':'{}','actual_cost':4})!=normalize(old)
    assert normalize({**old,'result_transfer_json':'{}','result_json':'["new-url"]'})!=normalize(old)


def test_existing_delivery_metadata_is_never_ignored():
    normalize=snapshot_row()
    old={'id':'too_existing','result_transfer_json':'{"status":"ready","items":["stored-object"]}'}
    assert normalize(old)==old
    assert normalize({**old,'result_transfer_json':'{}'})!=normalize(old)
    assert normalize({**old,'result_transfer_json':None})!=normalize(old)
