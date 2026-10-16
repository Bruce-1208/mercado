"""Re-run the pre-existing failing tests with this task's edits removed in memory."""
import sys,ast
from pathlib import Path
root=Path(__file__).resolve().parents[2];sys.path.insert(0,str(root))
source=(root/'scripts/test_capacity.py').read_text()
patch='''
    import ast
    from bit import bit_mysql as db
    baseline = Path('artifacts/stress-repair-20261011/bit_mysql.before.py').read_text()
    node = next(n for n in ast.parse(baseline).body if isinstance(n, ast.FunctionDef) and n.name == 'list_orders')
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'artifacts/stress-repair-20261011/bit_mysql.before.py', 'exec'), db.__dict__)
    current = Path('bit/bit_interface.py').read_text()
    current = current.replace('    if getattr(g, "workbench_database_unavailable", False):\\n        return None\\n', '')
    current = current.replace('        from bit.database_availability import unavailable_response\\n        unavailable = unavailable_response(exc)\\n        if unavailable is not None:\\n            return unavailable\\n', '')
    names = {'get_current_workbench_user', 'api_orders', 'api_inventory_stocks', 'api_store_links', 'api_prohibited_listings', 'api_mercado_products', 'api_1688_products'}
    for node in ast.parse(current).body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            node.decorator_list = [d for d in node.decorator_list if not (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == 'route')]
            exec(compile(ast.Module(body=[node], type_ignores=[]), '<baseline-route>', 'exec'), bit_interface.__dict__)
            if node.name in bit_interface.app.view_functions:
                bit_interface.app.view_functions[node.name] = getattr(bit_interface, node.name)
'''
source=source.replace('    import pytest\n',patch+'    import pytest\n')
source=source.replace("Path(__file__).resolve().parents[1]",repr(str(root))+" and Path("+repr(str(root))+")")
namespace={'__name__':'__main__','__file__':str(root/'scripts/test_capacity.py')}
exec(compile(source,'<baseline-runner>','exec'),namespace)
