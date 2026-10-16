"""Offline main activity multi-stop controls, without any marketplace requests."""
import json
import pytest
from test_promotion_pagination_browser import PromotionPaginationMock, pagination_page, console_browser, ROOT


@pytest.mark.parametrize('width',[1440,1024,390])
def test_other_campaigns_remain_selectable_while_stop_runs(pagination_page,width):
    page=pagination_page
    page.set_viewport_size({'width':width,'height':900})
    mock=PromotionPaginationMock(page)
    page.evaluate("""() => {
        const base=state.campaigns[0];
        state.campaigns=[{...base,id:1,name:'平台大促',active_item_count:120,participated:true},
            {...base,id:2,name:'智能共补',promotion_type:'SMART',active_item_count:10},
            {...base,id:3,name:'闪购',promotion_type:'LIGHTNING',active_item_count:8},
            {...base,id:4,name:'清仓',promotion_type:'CLEARANCE',active_item_count:5}];
        renderCampaigns();
    }""")
    campaigns=page.evaluate('state.campaigns')
    submissions=[]
    def stop(route):
        ids=route.request.post_data_json['promotion_ids'];submissions.append(ids)
        mock.json(route,{'results':[{'job_id':f'job-{i}','promotion_id':i,'status':'running'} for i in ids]})
    page.route('**/api/promotions/stop*',stop)
    page.route('**/api/promotions?*',lambda route:mock.json(route,{'rows':campaigns,'summary':{'total':4,'candidate_items':0,'active_items':143,'ending_soon':0}}))
    page.route('**/api/promotions/jobs?*',lambda route:mock.json(route,{'rows':[{'job_id':f'job-{i}','promotion_fk':i,'status':'running','total_count':10} for i in [1,2,3]]}))
    page.on('dialog',lambda dialog:dialog.accept())
    first=page.get_by_role('checkbox',name='选择活动 平台大促',exact=True)
    first.focus();page.keyboard.press('Space')
    assert first.is_checked()
    page.get_by_role('checkbox',name='选择活动 智能共补',exact=True).check()
    page.locator('#stopCampaignsBtn').click()
    page.wait_for_function('state.stopJobs?.size===2 && !state.stopSubmitting')
    assert submissions==[[1,2]]
    assert page.get_by_role('checkbox',name='选择活动 平台大促',exact=True).is_disabled()
    assert page.get_by_role('checkbox',name='选择活动 清仓',exact=True).is_disabled()
    lightning=page.get_by_role('checkbox',name='选择活动 闪购',exact=True)
    assert lightning.is_enabled()
    lightning.check()
    assert page.locator('#stopCampaignsBtn').is_enabled()
    page.locator('#stopCampaignsBtn').click()
    page.wait_for_function('state.stopJobs?.size===3 && !state.stopSubmitting')
    assert submissions==[[1,2],[3]]
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    path=ROOT/'artifacts/promotion-stop-20261012';path.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(path/f'stop-{width}.png'),full_page=True)
    assert not mock.errors
