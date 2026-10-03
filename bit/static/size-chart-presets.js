(function (root, factory) {
  'use strict';
  const catalog = factory();
  if (typeof module === 'object' && module.exports) module.exports = catalog;
  else root.SizeChartPresets = catalog;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  // These are editable starting labels and measurement instructions, never
  // official attribute IDs, measured values, or conversions between systems.
  const version = '2026.10.01';
  const series = (start, end, step = 1, prefix = '', suffix = '') => {
    const labels = [];
    for (let value = start; value <= end; value += step) labels.push(`${prefix}${value}${suffix}`);
    return labels;
  };
  const system = (id, name, labels) => ({id, name, labels});
  const measure = (name, note, unit = 'cm') => ({name, note, unit});
  const alpha = () => system('alpha', '字母尺码 · 按商品删减', ['XXS', 'XS', 'S', 'M', 'L', 'XL', '2XL', '3XL', '4XL', '5XL']);
  const womensNumeric = () => [
    system('eu_women', 'EU 女装数字标识 · 独立体系', series(32, 54, 2)),
    system('us_women', 'US 女装数字标识 · 独立体系', series(0, 24, 2)),
    system('uk_women', 'UK 女装数字标识 · 独立体系', series(4, 26, 2)),
    system('br_women', 'BR 女装数字标识 · 独立体系', series(34, 54, 2))
  ];
  const mensNumeric = () => [
    system('eu_men', 'EU 男装数字标识 · 独立体系', series(44, 64, 2)),
    system('us_men', 'US 男装数字标识 · 独立体系', series(34, 54, 2))
  ];
  const nominalWaist = () => system('waist_label', 'W 腰码标识 · 核对品牌定义', series(24, 48, 1, 'W'));
  const heightChildren = () => system('height_label', '身高型尺码标识 · 按品牌核对', series(80, 170, 10));
  const bodyChest = measure('胸围（人体）', '软尺水平绕胸部最丰满处一周，自然呼吸；这是人体围度，不是成衣平铺胸宽。');
  const bodyWaist = measure('腰围（人体）', '软尺水平绕商品指定腰位一周，注明自然腰或低腰；不要用裤腰平铺宽度替代。');
  const bodyHip = measure('臀围（人体）', '软尺水平绕臀部最丰满处一周，保持软尺贴合但不勒紧。');
  const chestWidth = measure('胸宽（成衣平铺）', '成衣扣合并自然平铺，测左右腋下之间的单面宽度；不要直接当作胸围。');
  const shoulder = measure('肩宽（成衣平铺）', '成衣背面平铺，测左右肩缝之间距离；落肩、插肩结构应另注明测量起止点。');
  const sleeve = measure('袖长（成衣）', '沿袖子测肩缝至袖口；插肩袖可从后领中点测至袖口，并明确所用方法。');
  const garmentLength = measure('衣长（成衣）', '从肩部最高点至下摆测量，注明是否包含领、帽或背部较长的下摆。');
  const waistWidth = measure('腰宽（成衣平铺）', '腰头自然放松平铺，测左右两端的单面宽度；弹性腰另测拉伸状态，不自动换算成围度。');
  const hipWidth = measure('臀宽（成衣平铺）', '按固定的臀部位置横向测单面宽度，并注明离腰头的测量位置。');
  const inseam = measure('内侧裤长（成衣）', '从裆部内侧接缝沿裤腿内侧测至裤脚，不包含腰头。');
  const trousersLength = measure('外侧裤长（成衣）', '从腰头上沿沿裤腿外侧测至裤脚，注明是否包含腰头。');
  const footLength = measure('足长（人体）', '站立承重，测脚跟至最长脚趾的距离，双脚分别测量并按较长脚选码；不要填鞋垫或鞋内长度。');
  const footWidth = measure('足宽（人体）', '站立承重，测前脚掌最宽处；注明横向直线宽度，不与脚围混用。');
  const insoleLength = measure('鞋内长／鞋垫长（商品）', '仅按实际鞋款测量可用鞋内长或可取出鞋垫的长度，并注明方法；不得从足长自动加余量生成。');
  const headGirth = measure('头围（人体）', '软尺经眉上、耳上与后脑最凸处水平绕一周；按品牌佩戴位置复核。');
  const palmGirth = measure('掌围（人体）', '软尺绕掌部指关节处一周，通常不包含拇指；注明是否按商品规定的方法包含拇指。');
  const handLength = measure('手长（人体）', '测腕部规定起点至中指指尖的直线距离，并记录测量起点。');
  const dimensions = [
    measure('长（商品）', '测实际商品的最长方向，明确展开、闭合或装配状态；外包装尺寸另行记录。'),
    measure('宽（商品）', '测与长度垂直的实际商品宽度，明确是否包含突出部件。'),
    measure('高（商品）', '按正常摆放方向测实际商品高度，明确是否包含脚座、把手或其他突出部件。')
  ];
  const preset = (id, name, group, description, domain_hints, measure_type, measurements, size_systems) => ({id, name, group, description, domain_hints, measure_type, measurements, size_systems});
  const independent = '各体系独立使用，不表示等码关系；标签仅是起始清单，须按品牌、商品及官方可选值调整。';
  const presets = [
    preset('adult_tops', '成人上衣／T 恤／衬衫', '成人服装', independent, ['上衣', 'T 恤', '衬衫', 'shirts', 'tops'], 'CLOTHING_MEASURE', [chestWidth, shoulder, sleeve, garmentLength], [alpha(), ...womensNumeric(), ...mensNumeric()]),
    preset('adult_coats', '成人外套／夹克／大衣', '成人服装', '厚度、内搭与款式影响穿着松量，请提供该款成衣实测数据。' + independent, ['外套', '夹克', '大衣', 'jackets', 'coats'], 'CLOTHING_MEASURE', [chestWidth, shoulder, sleeve, garmentLength, measure('下摆宽（成衣平铺）', '外套闭合后自然平铺，测下摆左右两端的单面宽度。')], [alpha(), ...womensNumeric(), ...mensNumeric()]),
    preset('adult_pants', '成人长裤／短裤', '成人服装', '腰码、版型与腰位分别核对，弹性款记录自然放松状态。' + independent, ['长裤', '短裤', 'trousers', 'pants', 'shorts'], 'CLOTHING_MEASURE', [waistWidth, hipWidth, inseam, trousersLength, measure('裤脚宽（成衣平铺）', '裤腿自然平铺，测裤脚左右两端的单面宽度。')], [alpha(), nominalWaist(), ...womensNumeric(), ...mensNumeric()]),
    preset('adult_jeans', '牛仔裤', '成人服装', 'W 与 L 是品牌标识；W/L 组合、实际腰宽与裤长分别填写，不按标签生成测量值。' + independent, ['牛仔裤', 'jeans'], 'CLOTHING_MEASURE', [waistWidth, hipWidth, inseam, trousersLength, measure('前裆长（成衣）', '从前腰头上沿沿前裆缝测至裆部交点，注明是否包含腰头。')], [nominalWaist(), system('waist_inseam_label', 'W/L 组合标识 · 按实际供应选择', series(26, 38, 2, 'W').flatMap(waist => ['L28', 'L30', 'L32', 'L34'].map(length => `${waist} ${length}`))), alpha(), ...womensNumeric()]),
    preset('dresses', '连衣裙／礼服', '成人服装', '修身、宽松与弹性款分开维护，裙长注明测量起点。' + independent, ['连衣裙', '礼服', 'dresses'], 'CLOTHING_MEASURE', [chestWidth, waistWidth, hipWidth, shoulder, sleeve, measure('裙长（成衣）', '从肩部最高点测至裙摆；吊带、无肩带或拖尾款需另注明起点与前后长度。')], [alpha(), ...womensNumeric()]),
    preset('skirts', '半身裙', '成人服装', '记录裙腰位置，前后不等长或带弹性款需注明测量状态。' + independent, ['半身裙', 'skirts'], 'CLOTHING_MEASURE', [waistWidth, hipWidth, measure('裙长（成衣）', '从腰头上沿测至裙摆，注明是否包含腰头；不等长款注明前后长度。')], [alpha(), ...womensNumeric()]),
    preset('suits', '西装／套装', '成人服装', '上装与下装分别实测，组合套装不能只用一件的测量值代表整套。' + independent, ['西装', '套装', 'suits'], 'CLOTHING_MEASURE', [chestWidth, shoulder, sleeve, garmentLength, waistWidth, hipWidth, inseam, trousersLength], [alpha(), ...mensNumeric(), ...womensNumeric()]),
    preset('sportswear', '运动服／健身服', '成人服装', '压缩、弹性和宽松款分开维护；自然放松与拉伸后的成衣数据不要混填。' + independent, ['运动服', '健身服', 'sportswear', 'activewear'], 'CLOTHING_MEASURE', [chestWidth, garmentLength, waistWidth, hipWidth, inseam, measure('弹性说明', '记录材质弹性、压缩用途与测量状态；这是说明文字，单位和官方字段以实时规格为准。', '')], [alpha(), ...womensNumeric(), ...mensNumeric()]),
    preset('swimwear', '泳装／泳裤', '成人服装', '连体、分体和泳裤各自实测；采用品牌规定的身体测量方法。' + independent, ['泳装', '泳裤', 'swimwear'], 'BODY_MEASURE', [bodyChest, bodyWaist, bodyHip, measure('躯干围（人体）', '仅在品牌要求时，软尺从肩部经前身、裆部与后背回到起点绕一周；不与肩到裆的直线长度混用。')], [alpha(), ...womensNumeric(), nominalWaist()]),
    preset('bras', '文胸／胸罩', '内衣', '下胸围码与杯型按品牌分别核对；US、UK 与 EU 杯型名称不能自动互换。' + independent, ['文胸', '胸罩', 'bras'], 'BODY_MEASURE', [bodyChest, measure('下胸围（人体）', '软尺在乳房下方保持水平绕一周，自然呼吸；不从杯码或胸围差自动填入数值。')], [
      system('eu_band_cup', 'EU 胸围码＋杯型 · 独立标识', series(65, 95, 5).flatMap(band => ['A', 'B', 'C', 'D', 'E', 'F'].map(cup => `${band}${cup}`))),
      system('us_band_cup', 'US 胸围码＋杯型 · 独立标识', series(30, 42, 2).flatMap(band => ['A', 'B', 'C', 'D', 'DD', 'DDD'].map(cup => `${band}${cup}`))),
      system('uk_band_cup', 'UK 胸围码＋杯型 · 独立标识', series(28, 40, 2).flatMap(band => ['A', 'B', 'C', 'D', 'DD', 'E', 'F'].map(cup => `${band}${cup}`))), alpha()
    ]),
    preset('underwear', '内裤／贴身内衣', '内衣', '以商品实际采用的体系标记尺码，人体围度与成衣宽度分别测量。' + independent, ['内裤', '内衣', 'underwear'], 'BODY_MEASURE', [bodyWaist, bodyHip], [alpha(), nominalWaist(), ...womensNumeric()]),
    preset('baby_clothing', '婴儿服装／连体衣', '婴童', '月龄与身高只是品牌尺码标识；年龄不保证适合体型，优先核对该款实际尺寸。' + independent, ['婴儿服装', '连体衣', 'baby clothing'], 'CLOTHING_MEASURE', [chestWidth, garmentLength, sleeve, inseam, measure('肩至裆长（成衣）', '连体衣扣合并自然平铺，从肩部最高点测至裆部；尿布空间按该款设计核对。')], [system('month_label', '月龄标识 · 按品牌核对', ['新生儿', '0–3 月', '3–6 月', '6–9 月', '9–12 月', '12–18 月', '18–24 月']), system('height_label', '身高型标识 · 按品牌核对', series(50, 98, 6))]),
    preset('kids_clothing', '儿童／青少年服装', '婴童', '年龄与身高分别作为标识，不自动互相换算；选择适合该款的测量项目。' + independent, ['童装', '青少年服装', 'kids clothing'], 'CLOTHING_MEASURE', [chestWidth, shoulder, sleeve, garmentLength, waistWidth, hipWidth, inseam], [heightChildren(), system('age_label', '年龄标识 · 按品牌核对', ['2 岁', '3 岁', '4 岁', '5 岁', '6 岁', '7 岁', '8 岁', '9 岁', '10 岁', '12 岁', '14 岁', '16 岁']), system('alpha_kids', '儿童字母标识 · 与成人尺码分开', ['XS', 'S', 'M', 'L', 'XL'])]),
    preset('adult_footwear', '成人鞋／靴／凉鞋', '鞋袜', '男鞋、女鞋与各地区鞋码分别维护；尺码与足长、鞋内长不存在本模板提供的转换。' + independent, ['鞋', '靴', '凉鞋', 'shoes', 'footwear'], 'MIXED_MEASURE', [footLength, footWidth, insoleLength, measure('靴筒高（商品）', '仅适用于靴款，从规定起点测至靴筒上沿，注明是否包含鞋跟。')], [
      system('eu', 'EU 成人鞋码 · 独立体系', series(35, 48)),
      system('us_women', 'US 女鞋码 · 独立体系', series(5, 12, 0.5)),
      system('us_men', 'US 男鞋码 · 独立体系', series(6, 15, 0.5)),
      system('uk', 'UK 成人鞋码 · 独立体系', series(3, 13, 0.5)),
      system('br', 'BR 成人鞋码 · 独立体系', series(33, 46))
    ]),
    preset('kids_footwear', '儿童／青少年鞋', '鞋袜', '童鞋阶段的 C、Y 等后缀保留，不能与成人鞋码或无后缀数字混为一体。' + independent, ['童鞋', 'kids shoes'], 'MIXED_MEASURE', [footLength, footWidth, insoleLength], [system('eu_kids', 'EU 童鞋码 · 独立体系', series(20, 39)), system('us_child', 'US 儿童鞋码 C · 按品牌核对', series(5, 13, 0.5, '', 'C')), system('us_youth', 'US 青少年鞋码 Y · 按品牌核对', series(1, 7, 0.5, '', 'Y')), system('uk_kids', 'UK 儿童鞋码 · 按品牌核对', series(4, 13, 0.5))]),
    preset('baby_footwear', '婴儿鞋／学步鞋', '鞋袜', '月龄标签不能代替足长，学步鞋需测量实际穿着者足部与鞋款。' + independent, ['婴儿鞋', '学步鞋', 'baby shoes'], 'MIXED_MEASURE', [footLength, footWidth, insoleLength], [system('eu_baby', 'EU 婴儿鞋码 · 按品牌核对', series(16, 24)), system('us_baby', 'US 婴儿鞋码 · 按品牌核对', series(1, 5, 0.5)), system('month_label', '月龄标识 · 按品牌核对', ['0–3 月', '3–6 月', '6–9 月', '9–12 月', '12–18 月', '18–24 月'])]),
    preset('socks', '袜子／长筒袜', '鞋袜', '袜码范围是品牌的标签，不能直接套用鞋码范围推算袜子长度。' + independent, ['袜子', '长筒袜', 'socks'], 'CLOTHING_MEASURE', [measure('袜底长（成衣平铺）', '袜子自然放松平铺，从袜跟至袜尖测量；与人体足长分开记录。'), measure('袜筒长（成衣平铺）', '自然放松平铺，从袜跟或品牌指定起点至袜口测量，并注明起点。'), measure('袜口宽（成衣平铺）', '袜口自然放松平铺，测单面宽度；另有拉伸数据时明确标记。')], [system('alpha', '字母／均码标识 · 按品牌核对', ['XS', 'S', 'M', 'L', 'XL', '均码']), system('eu_range_label', '品牌 EU 范围标签示例 · 须按商品修改', ['35–38', '39–42', '43–46'])]),
    preset('hats', '帽子／头饰', '配饰', '均码、调节范围与固定尺码分别描述；头围标识须核对品牌测量口径。' + independent, ['帽子', '头饰', 'hats'], 'BODY_MEASURE', [headGirth, measure('可调节头围下限（商品）', '实际调节到最小的可用内部围度，按品牌方法测量；不从标签自动生成。'), measure('可调节头围上限（商品）', '实际调节到最大的可用内部围度，与下限使用相同测量方法。')], [system('alpha', '字母／均码标识', ['XS', 'S', 'M', 'L', 'XL', '均码']), system('head_label', '头围型标识 · 按品牌核对', series(52, 64))]),
    preset('gloves', '手套', '配饰', '掌围、手长与手套成品长度分别记录；左右手不同尺寸时按商品规则选码。' + independent, ['手套', 'gloves'], 'BODY_MEASURE', [palmGirth, handLength, measure('中指长（人体）', '从中指根部的品牌指定位置测至指尖，避免与整只手的长度混用。')], [system('alpha', '字母／均码标识', ['XS', 'S', 'M', 'L', 'XL', '2XL', '均码']), system('numeric_label', '数字手套码 · 按品牌核对', series(6, 11, 0.5))]),
    preset('belts', '腰带／皮带', '配饰', '腰带标称长度、总长与扣针至孔位长度可能不同，需记录该款定义。' + independent, ['腰带', '皮带', 'belts'], 'MIXED_MEASURE', [bodyWaist, measure('扣针至中间孔长度（商品）', '腰带平直放置，从扣针的规定起点至中间孔测量；无孔款按品牌方法记录可用长度。'), measure('腰带总长（商品）', '平直测量总长，注明是否包含扣头，不能替代扣针至孔位的有效长度。'), measure('腰带宽（商品）', '在带身平整处测横向宽度，扣头宽度另行记录。')], [alpha(), system('length_label', '标称长度标识 · 须核对品牌口径', series(65, 130, 5)), system('waist_label', '英寸腰码标识 · 须核对品牌口径', series(28, 48, 2))]),
    preset('rings', '戒指', '配饰', '各地区戒指码单独记录；内径与内周长需要实测，不从码数或其他体系自动计算。' + independent, ['戒指', 'rings'], 'MIXED_MEASURE', [measure('戒指内径（商品）', '测戒指内部圆孔的直径，避开圈壁；非圆形款需使用品牌指定方法。', 'mm'), measure('戒指内周长（商品）', '按品牌规定方法测内部一周的长度；不与外周长或圈壁厚度混用。', 'mm'), measure('手指围（人体）', '测佩戴位置的手指周长，并按品牌要求考虑关节与佩戴状态；不当作戒指实测内周长。', 'mm')], [system('us', 'US 戒指码 · 独立体系', series(3, 13, 0.5)), system('eu', 'EU 戒指码标识 · 独立体系', series(44, 70)), system('hk', '香港戒指码标识 · 独立体系', series(5, 30)), system('jp', '日本戒指码标识 · 独立体系', series(1, 30))]),
    preset('bags_luggage', '箱包／背包／手袋', '其他商品', '容量、箱体标称码与实际外尺寸分开填写；尺寸标签以该款定义为准。' + independent, ['箱包', '背包', '手袋', 'bags', 'luggage'], 'CLOTHING_MEASURE', [...dimensions, measure('肩带长度（商品）', '按品牌指定起点测肩带长度；可调款分别测最短与最长状态。')], [system('alpha', '大小／均码标识 · 按商品修改', ['XS', 'S', 'M', 'L', 'XL', '均码']), system('numeric_label', '品牌数字规格标识 · 按商品修改', ['1', '2', '3', '4', '5'])]),
    preset('home_textiles', '床品／家居纺织品', '其他商品', '单人、双人等名称各市场定义不同，床垫尺寸与成品尺寸分开填写。' + independent, ['床品', '家居纺织品', 'bedding', 'home textiles'], 'CLOTHING_MEASURE', [measure('成品长（商品）', '成品自然平铺测长度，注明是否包含边饰；采用品牌规定的洗前或洗后状态。'), measure('成品宽（商品）', '成品自然平铺测宽度，与长度保持同一测量状态。'), measure('适用床垫厚度（规格）', '仅适用于床笠等款式，依据真实适配资料记录；不要从床品标签推算。')], [system('bed_label', '床型名称标识 · 按市场核对', ['Single', 'Twin', 'Double', 'Full', 'Queen', 'King']), system('custom_label', '自定义规格标识 · 按商品修改', ['规格 A', '规格 B', '规格 C'])]),
    preset('general_products', '通用商品／自定义规格', '其他商品', '用于补充未列出的商品类型。尺寸、重量或容量需由实际商品资料提供，官方未支持的类目不能据此创建平台尺码表。', [], 'CLOTHING_MEASURE', dimensions, [system('alpha', '大小／均码标识 · 按商品修改', ['XS', 'S', 'M', 'L', 'XL', '均码']), system('numeric_label', '数字规格标识 · 按商品修改', ['1', '2', '3', '4', '5']), system('custom_label', '自定义规格标识 · 按商品修改', ['规格 A', '规格 B', '规格 C'])])
  ];

  function deepFreeze(value) {
    if (value && typeof value === 'object' && !Object.isFrozen(value)) {
      Object.values(value).forEach(deepFreeze);
      Object.freeze(value);
    }
    return value;
  }
  const byId = new Map(presets.map(item => [item.id, item]));
  const getPreset = id => byId.get(id) || null;
  const getSizeSystem = (presetId, systemId) => getPreset(presetId)?.size_systems.find(item => item.id === systemId) || null;
  return deepFreeze({version, presets, getPreset, getSizeSystem});
});
