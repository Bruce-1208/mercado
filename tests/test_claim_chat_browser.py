"""Offline browser regressions for progressive claim chat."""
from pathlib import Path
import pytest
from test_console_ui_browser import console_browser, console_page


def setup_claim(page):
    page.evaluate("""() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-customer-service').classList.add('active');
        hasWorkbenchPermission = () => true;
        mercadoStoreTokenRows = [{id:7, display_name:'测试店铺', meli_user_id:'123', site_id:'CBT'}];
        customerServiceRows = [{id:'100', token_id:7}];
        window.claimCalls = [];
        window.extrasPromise = new Promise(resolve => window.resolveExtras = resolve);
        window.translationPromise = new Promise(resolve => window.resolveTranslation = resolve);
        window.sendPromise = new Promise(resolve => window.resolveSend = resolve);
        customerServiceRequest = async (action, options) => {
            claimCalls.push({action, data:options.data});
            if (action === 'claims-send') return sendPromise;
            if (action === 'claims-translate') return translationPromise;
            if (options.data.section === 'extras') return extrasPromise;
            return {claim:{id:options.data.claim_id, site_id:'MLB', players:[{type:'seller', role:'respondent', user_id:'123', available_actions:[{action:'send_message_to_complainant'}]}]},
                messages:[{message:'Olá, preciso de ajuda.', sender_role:'complainant', receiver_role:'respondent'}], resource_errors:{}, extras_pending:true};
        };
        openCustomerServiceDetail('订单索赔 #100', '测试店铺');
        loadClaimDetail('100',7);
    }""")
    page.locator('#customer-service-claim-text').wait_for()


@pytest.mark.parametrize('width', [1440,390])
def test_chat_and_composer_available_before_extras_preserve_draft_and_cache(console_page, width):
    page = console_page
    page.set_viewport_size({'width':width,'height':1000})
    setup_claim(page)
    textarea = page.locator('#customer-service-claim-text')
    textarea.fill('请放心，我们会帮助您解决问题。')
    assert '正在翻译为中文' in page.locator('.customer-service-chat').inner_text()
    page.evaluate("resolveTranslation({translations:['您好，我需要帮助。']})")
    page.wait_for_function("document.querySelector('.customer-service-chat').textContent.includes('您好，我需要帮助。')")
    page.evaluate("resolveExtras({detail:{title:'处理说明',description:'处理中'},reason:{detail:'买家需要帮助'}, affects_reputation:{affects_reputation:false}, expected_resolutions:[],resource_errors:{}})")
    page.wait_for_function("document.getElementById('customer-service-claim-reason').textContent.includes('买家需要帮助')")
    assert textarea.input_value() == '请放心，我们会帮助您解决问题。'
    assert 'Olá, preciso de ajuda.' in page.locator('.customer-service-chat').inner_text()
    assert page.locator('#customer-service-post-translated').count() == 0
    directory = Path(__file__).resolve().parents[1] / 'artifacts/claim-chat-20261008'
    directory.mkdir(parents=True, exist_ok=True)
    page.locator('#customer-service-detail-dialog').screenshot(path=str(directory / f'claim-{width}.png'))
    page.evaluate("closeCustomerServiceDetail(); openCustomerServiceDetail('订单索赔 #100'); loadClaimDetail('100',7)")
    assert page.evaluate("claimCalls.filter(call=>call.data.section === 'metadata').length") == 1
    assert '您好，我需要帮助。' in page.locator('.customer-service-chat').inner_text()


def test_old_claim_response_does_not_overwrite_new_claim(console_page):
    page = console_page
    setup_claim(page)
    page.evaluate("""() => {
        window.oldCore = new Promise(resolve => window.resolveOldCore = resolve);
        const original = customerServiceRequest;
        customerServiceRequest = (action, options) => options.data.claim_id === '101' && options.data.section === 'metadata' ? oldCore : original(action,options);
        loadClaimDetail('101',7);
        loadClaimDetail('102',7);
    }""")
    page.wait_for_function("customerServiceClaimBundle?.claim.id === '102'")
    page.evaluate("resolveOldCore({claim:{id:'101'}, messages:[{message:'WRONG CLAIM'}]})")
    assert page.evaluate('customerServiceClaimBundle.claim.id') == '102'
    assert 'WRONG CLAIM' not in page.locator('#customer-service-detail-dialog').inner_text()


def test_sending_chinese_sets_auto_translation_and_prevents_duplicate_clicks(console_page):
    page = console_page
    setup_claim(page)
    page.locator('#customer-service-claim-text').fill('我们会帮助您。')
    page.evaluate("sendClaimMessage('100','complainant',7); sendClaimMessage('100','complainant',7)")
    assert page.locator('#customer-service-claim-send').is_disabled()
    calls = page.evaluate("claimCalls.filter(call=>call.action === 'claims-send')")
    assert len(calls) == 1
    assert calls[0]['data'] == {'claim_id':'100', 'receiver_role':'complainant', 'message':'我们会帮助您。', 'auto_translate':True}
    page.evaluate('resolveSend({})')
    page.wait_for_function("claimCalls.filter(call=>call.data.section === 'metadata').length === 2 && !document.getElementById('customer-service-claim-send').disabled")


def test_translation_failure_keeps_original_and_retry_preserves_reply(console_page):
    page = console_page
    setup_claim(page)
    page.locator('#customer-service-claim-text').fill('我的中文回复')
    page.evaluate("resolveTranslation({translations:[]})")
    page.wait_for_function("document.querySelector('.customer-service-chat').textContent.includes('中文翻译失败')")
    assert 'Olá, preciso de ajuda.' in page.locator('.customer-service-chat').inner_text()
    page.evaluate("translationPromise = Promise.resolve({translations:['您好，我需要帮助。']})")
    page.get_by_role('button', name='重试翻译', exact=True).click()
    page.wait_for_function("document.querySelector('.customer-service-chat').textContent.includes('您好，我需要帮助。')")
    assert page.locator('#customer-service-claim-text').input_value() == '我的中文回复'


def test_html_mediator_message_is_cleaned_before_display_and_translation(console_page):
    page = console_page
    setup_claim(page)
    page.evaluate('resolveTranslation({translations:[]})')
    page.wait_for_function("!customerServiceClaimBundle._translation_request")
    page.evaluate("""() => {
        window.translationPromise = Promise.resolve({translations:['您好，买家报告商品损坏。']});
        customerServiceClaimBundle.messages = [{message:'<p dir="ltr" style="text-align:start">Olá, <b>vendedor!</b></p><p>O produto chegou quebrado.</p><script>window.unsafeMarkup=true</script>'}];
        renderClaimDetail(customerServiceClaimBundle,7);
        retryClaimTranslation();
    }""")
    page.wait_for_function("document.querySelector('.customer-service-chat').textContent.includes('买家报告商品损坏')")
    chat = page.locator('.customer-service-chat').inner_text()
    assert 'Olá, vendedor!' in chat and 'O produto chegou quebrado.' in chat
    assert '<p' not in chat and 'style=' not in chat and 'unsafeMarkup' not in chat
    assert not page.evaluate('Boolean(window.unsafeMarkup)')
    texts = page.evaluate("claimCalls.filter(c=>c.action==='claims-translate').at(-1).data.texts")
    assert texts == ['Olá, vendedor!\nO produto chegou quebrado.']
    assert page.evaluate("customerServicePlainText('&lt;p&gt;Olá &amp; bom dia&lt;/p&gt;')") == 'Olá & bom dia'


@pytest.mark.parametrize('width', [1440, 390])
def test_buyer_attachments_render_in_claim_and_post_sale_chat(console_page, width):
    import base64
    page = console_page
    page.set_viewport_size({'width':width, 'height':1000})
    page.route('**/claims-attachment?*', lambda route: route.fulfill(
        content_type='image/png', body=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jB1sAAAAASUVORK5CYII=')))
    setup_claim(page)
    page.evaluate("""() => {
        customerServiceClaimBundle.messages = [{message:'您好', attachments:[{filename:'buyer.png',original_filename:'买家图片.png'}, {filename:'receipt.pdf'}, {filename:'../unsafe.jpg'}], sender_role:'complainant'}];
        renderClaimDetail(customerServiceClaimBundle,7);
        // A translation may finish before the lazy image has loaded.
        updateClaimChat(customerServiceClaimBundle);
    }""")
    image = page.locator('.customer-service-chat img')
    page.wait_for_function("document.querySelector('.customer-service-chat img')?.naturalWidth > 0")
    assert image.is_visible()
    assert page.locator('.attachment-error:visible').count() == 0
    assert page.locator('.customer-service-attachment').count() == 2
    assert 'claim_id=100' in image.get_attribute('src')
    assert page.locator('.customer-service-attachment a').first.get_attribute('target') == '_blank'
    box = image.bounding_box()
    assert box['width'] <= 260
    assert page.evaluate("""() => {
        const original = document.querySelector('.customer-service-chat img');
        updateClaimChat(customerServiceClaimBundle);
        return original === document.querySelector('.customer-service-chat img');
    }""")
    page.evaluate("renderPostSaleDetail('200',[{text:'您好', site_id:'MLB', attachments:['buyer.jpg']}],{},7,'123')")
    src = page.locator('.customer-service-chat img').get_attribute('src')
    assert '/7/post-sale-attachment?' in src and 'site_id=MLB' in src and 'pack_id=200' in src
    page.wait_for_function("document.querySelector('.customer-service-chat img')?.hidden")
    assert page.locator('.customer-service-chat img').is_hidden()
    assert page.locator('.attachment-error').is_visible()


def test_background_translation_poll_and_timeout_do_not_leave_spinner(console_page):
    page = console_page
    setup_claim(page)
    page.evaluate('resolveTranslation({translations:[]})')
    page.wait_for_function("!customerServiceClaimBundle._translation_request")
    page.evaluate("""() => {
        const original = customerServiceRequest;
        window.pollCount = 0;
        customerServiceRequest = async (action, options) => action !== 'claims-translate' ? original(action, options) : (++pollCount === 1 ? {pending:true} : {translations:['您好，我需要帮助。']});
        retryClaimTranslation();
    }""")
    page.wait_for_function("document.querySelector('.customer-service-chat').textContent.includes('您好，我需要帮助。')")
    assert page.evaluate('pollCount') == 2
    page.evaluate("""() => {
        delete customerServiceClaimBundle.messages[0]._text_zh;
        customerServiceRequest = async () => ({pending:true});
        const now = Date.now;
        let ticks = 0;
        Date.now = () => now() + (ticks++ * 100000);
        retryClaimTranslation().finally(() => Date.now = now);
    }""")
    page.wait_for_function("document.querySelector('.customer-service-chat').textContent.includes('翻译耗时较长')")
    assert '正在翻译为中文' not in page.locator('.customer-service-chat').inner_text()
    assert 'Olá, preciso de ajuda.' in page.locator('.customer-service-chat').inner_text()


def test_translation_http_timeout_is_reported(console_page):
    page = console_page
    result = page.evaluate("""async () => {
        const original = window.fetch;
        window.fetch = (_url, options) => new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError'))));
        try {
            await customerServiceRequest('claims-translate', {tokenId:7, method:'POST', timeoutMs:20});
        } catch (error) { return error.message; }
        finally { window.fetch = original; }
    }""")
    assert result == '读取超时，请重试'


def test_long_chat_translation_uses_bounded_batches(console_page):
    page = console_page
    setup_claim(page)
    page.evaluate('resolveTranslation({translations:[]})')
    page.wait_for_function("!customerServiceClaimBundle._translation_request")
    page.evaluate("""() => {
        window.translationBatches = [];
        customerServiceClaimBundle.messages = Array.from({length:23}, (_, i) => ({message:'Message ' + i}));
        const original = customerServiceRequest;
        customerServiceRequest = async (action, options) => {
            if (action !== 'claims-translate') return original(action, options);
            translationBatches.push(options.data.texts);
            return {translations:options.data.texts.map(text => '中文 ' + text)};
        };
        return retryClaimTranslation();
    }""")
    assert page.evaluate('translationBatches.map(batch => batch.length)') == [10, 10, 3]
    assert page.evaluate('customerServiceClaimBundle.messages.every(message => message._text_zh === "中文 " + message.message)')


@pytest.mark.parametrize('width', [1440, 390])
def test_messages_and_images_arrive_before_slow_metadata(console_page, width):
    import base64
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.route('**/claims-attachment?*', lambda route: route.fulfill(
        content_type='image/png', body=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jB1sAAAAASUVORK5CYII=')))
    setup_claim(page)
    page.evaluate("""() => {
        const original = customerServiceRequest;
        window.slowMetadata = new Promise(resolve => window.finishMetadata = resolve);
        customerServiceRequest = (action, options) => {
            if (options.data.section === 'metadata') return slowMetadata;
            if (options.data.section === 'messages') return Promise.resolve({messages:[
                {message:'图片已收到', attachments:[{id:'opaque-photo', original_filename:'买家照片.png'}, {id:'opaque-mime', content_type:'image/png'}]}
            ]});
            return original(action, options);
        };
        loadClaimDetail('103', 7);
    }""")
    page.wait_for_function("document.querySelectorAll('.customer-service-chat img').length === 2 && [...document.querySelectorAll('.customer-service-chat img')].every(img => img.naturalWidth > 0)")
    assert '图片已收到' in page.locator('.customer-service-chat').inner_text()
    assert page.locator('.customer-service-chat img').first.get_attribute('loading') == 'eager'
    page.evaluate("window.firstImage = document.querySelector('.customer-service-chat img'); finishMetadata({claim:{id:'103',site_id:'MLB'}})")
    page.wait_for_function("!customerServiceClaimBundle.metadata_pending")
    assert page.evaluate("firstImage === document.querySelector('.customer-service-chat img')")


def test_translation_starts_second_batch_before_first_finishes(console_page):
    page = console_page
    setup_claim(page)
    page.evaluate('resolveTranslation({translations:[]})')
    page.wait_for_function("!customerServiceClaimBundle._translation_request")
    page.evaluate("""() => {
        customerServiceClaimBundle.messages = Array.from({length:11}, (_,i) => ({message:'Message ' + i}));
        window.batchResolvers = [];
        customerServiceRequest = () => new Promise(resolve => batchResolvers.push(resolve));
        retryClaimTranslation();
    }""")
    assert page.evaluate('batchResolvers.length') == 2
    page.evaluate("batchResolvers[1]({translations:['第二批已完成']})")
    page.wait_for_function("document.querySelector('.customer-service-chat').textContent.includes('第二批已完成')")
    page.evaluate("batchResolvers[0]({translations:Array(10).fill('第一批已完成')})")
    page.wait_for_function("!customerServiceClaimBundle._translation_request")


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_claim_image_selection_remove_and_failed_send_preserves_images(console_page, width):
    import base64
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    setup_claim(page)
    image = {'name': 'test.png', 'mimeType': 'image/png', 'buffer': base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII=')}
    page.locator('#customer-service-claim-images').set_input_files([image, image])
    assert page.locator('.claim-image-preview img').count() == 2
    page.get_by_role('button', name='移除图片 2', exact=True).click()
    assert page.locator('.claim-image-preview img').count() == 1
    page.locator('#customer-service-claim-images').focus()
    assert page.locator('#customer-service-claim-images').evaluate('(el) => el === document.activeElement')
    page.evaluate("sendPromise = Promise.reject(new Error('模拟发送失败')); sendPromise.catch(() => {})")
    page.locator('#customer-service-claim-send').click()
    page.wait_for_function("claimCalls.some(call => call.action === 'claims-send') && !document.getElementById('customer-service-claim-send').disabled")
    call = page.evaluate("claimCalls.find(call => call.action === 'claims-send').data")
    assert call['message'] == '' and len(call['images']) == 1
    assert page.locator('.claim-image-preview img').count() == 1
    assert page.locator('#customer-service-claim-images').is_enabled()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    directory = Path(__file__).resolve().parents[1] / 'artifacts/claim-images-20261012'
    directory.mkdir(parents=True, exist_ok=True)
    page.locator('#customer-service-detail-dialog').screenshot(path=str(directory / f'images-{width}.png'))
