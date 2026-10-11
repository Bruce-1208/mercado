from flask import Flask
import pytest
from bit import mercado_action_center as module


class Store:
    def __init__(self): self.reads=[]
    def list_mercado_store_tokens(self):
        return {'rows':[
            dict(id=1,organization_key='org',salesperson='A',group_name='G1'),
            dict(id=2,organization_key='org',salesperson='B',group_name='G2'),
            dict(id=3,organization_key='org',salesperson='C',group_name='G2'),
            dict(id=4,organization_key='org',salesperson='A',group_name='G3'),
            dict(id=5,organization_key='org',site_settings=[dict(salesperson='A',group_name='G3'),dict(salesperson='C',group_name='G1')]),
            dict(id=6,organization_key='org',salesperson='A',group_name='G1'),
            dict(id=7,organization_key='other',salesperson='A',group_name='G1'),
        ]}
    def list_mercado_operator_tasks(self,**kwargs):
        self.reads.append(('tasks',kwargs['token_ids']))
        return {'rows':[dict(id=i,token_id=i,topic='items') for i in kwargs['token_ids']]}
    def list_mercado_notification_events(self,**kwargs):
        self.reads.append(('events',kwargs['token_ids']))
        return {'rows':[dict(id=i,token_id=i) for i in kwargs['token_ids']]}
    def list_mercado_prohibited_listings(self,**kwargs):
        self.reads.append(('rights',kwargs['token_ids']))
        return {'rows':[dict(token_id=i,infraction_id=str(i)) for i in kwargs['token_ids']],'total':len(kwargs['token_ids'])}
    def list_mercado_today_work_states(self,**kwargs): return []


def client(monkeypatch,store):
    monkeypatch.setattr(module,'_refresh_order_watch_tasks',lambda *args:(set(),set()))
    def load(storage,tokens):
        return {'rows':[dict(token_id=t['id'],claim_id=str(t['id']),pack_id=str(t['id']),text='（附件消息）',has_text=False) for t in tokens],'errors':[]}
    monkeypatch.setattr(module,'_unanswered_claims',load)
    monkeypatch.setattr(module,'_unanswered_post_sale',load)
    app=Flask(__name__)
    app.register_blueprint(module.create_action_center_blueprint(
        login_required=lambda fn:fn,current_user=lambda:dict(organization_key='org'),
        authorized_token_ids=lambda user:[1,2,3,4,5,7],storage=store))
    return app.test_client()


@pytest.mark.parametrize('endpoint',['tasks','claims','post-sale'])
@pytest.mark.parametrize('query,expected',[
    ('salesperson=A&salesperson=B&group_name=G1&group_name=G2',[1,2]),
    ('salesperson=A&salesperson=A&group_name=G1',[1]),
    ('salesperson=A&salesperson=B&group_name=',[1,2,4,5]),
    ('salesperson=&group_name=G1&group_name=G2',[1,2,3,5]),
    ('salesperson=A&group_name=G1',[1]),  # Never combine different site settings.
    ('salesperson=unavailable&salesperson=removed',[ ]),
    ('salesperson=&group_name=',[1,2,3,4,5]),
])
def test_multi_filters_match_any_value_per_field_and_preserve_authorization(monkeypatch,endpoint,query,expected):
    store=Store()
    data=client(monkeypatch,store).get(f'/api/mercado/today/{endpoint}?{query}').json['data']
    assert [r['token_id'] for r in data['rows']]==expected
    assert data['store_pagination']['total']==len(expected)
    if endpoint=='tasks':
        assert [r['token_id'] for r in data['rights_holder_replies']]==expected
        assert [r['token_id'] for r in data['events']]==expected
        assert all(ids==expected for _,ids in store.reads)
        assert data['filter_options']==dict(salesperson=['A','B','C'],group_name=['G1','G2','G3'])
