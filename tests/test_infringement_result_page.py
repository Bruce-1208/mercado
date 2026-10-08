import sqlite3
import pytest
from bit.infringement_result_page import references, SOURCES

class Cursor:
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        for table, _, _ in SOURCES.values():
            self.db.execute(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY, added_to_products INTEGER, is_current INTEGER)')
            self.db.executemany(f'INSERT INTO {table} VALUES (?, ?, ?)', [(1,0,1),(2,0,1),(3,1,0),(4,0,1)])
    def execute(self, sql, params=()):
        sql=sql.replace('FORCE INDEX (`idx_erp_meli_product_added`)','').replace('%s','?')
        self.cursor=self.db.execute(sql,params)
    def fetchone(self):return dict(self.cursor.fetchone())
    def fetchall(self):return [dict(row) for row in self.cursor.fetchall()]

@pytest.mark.parametrize('direction',['asc','desc'])
def test_pages_have_stable_ties_exact_total_and_no_missing_or_duplicate_sources(direction):
    cursor=Cursor()
    rows=[]
    for page in range(1,7):
        result,total=references(cursor,list(SOURCES),page,3,direction)
        assert total==14
        rows.extend(result)
    expected=[{'source_type':source,'row_id':i} for source in SOURCES for i in (1,2,3,4)
              if not (i==3 and source in ('collection_list','pulled'))]
    expected.sort(key=lambda row: ((-1 if direction=='desc' else 1)*row['row_id'],row['source_type']))
    assert rows==expected
    cursor.db.close()


def test_live_state_reader_only_receives_selected_ids_after_connection_release(monkeypatch):
    from bit import bit_mysql
    from unittest.mock import MagicMock
    connection = MagicMock()
    monkeypatch.setattr(bit_mysql.pymysql,'connect',lambda **kw:connection)
    monkeypatch.setattr('erp.mercadolibre_collection_store.ensure_collection_tables',lambda c:None)
    refs=[{'source_type':'product_list','row_id':9},{'source_type':'product_list','row_id':7}]
    monkeypatch.setattr('bit.infringement_result_page.references',lambda *a:(refs,1000))
    def candidates(**kwargs):
        connection.close.assert_called_once()
        assert kwargs['items']==refs
        return iter([{'source_type':'product_list','row_id':i,'source_row_id':i,
                      'title':str(i),'product_id':str(i)} for i in (7,9)])
    monkeypatch.setattr(bit_mysql,'get_infringement_risk_candidates',candidates)
    result=bit_mysql.get_infringement_risk_results(page=2,limit=2,sources=['product_list'],
                                                 sort_by='row_id',rule_version='v1')
    assert [r['row_id'] for r in result['rows']]==[9,7]
    assert result['total']==1000 and result['pages']==500
    assert result['unchecked']==2 and result['summary_scope']=='page'
