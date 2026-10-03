(function (root, factory) {
  'use strict';
  const library = factory();
  if (typeof module === 'object' && module.exports) module.exports = library;
  else root.SizeChartBuiltins = library;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  // House reference profiles, authored for this library; not a national standard,
  // brand sizing chart, or measurements taken from the user's merchandise.
  const version = '2026.10.02';
  const note = '泽顺通用参考数据 v2026.10.02：系统预设的身体尺寸范围，单位 cm。适用于按此范围选码的商品；品牌默认 Generic，可在模板维护中修改。';
  const fields = {
    SIZE:'尺码', FILTRABLE_SIZE:'国际尺码', PANTS_FILTRABLE_SIZES:'裤装尺码',
    CHEST_CIRCUMFERENCE_FROM:'胸围下限', CHEST_CIRCUMFERENCE_TO:'胸围上限',
    WAIST_CIRCUMFERENCE_FROM:'腰围下限', WAIST_CIRCUMFERENCE_TO:'腰围上限',
    HIP_CIRCUMFERENCE_FROM:'臀围下限', HIP_CIRCUMFERENCE_TO:'臀围上限',
    PERSON_HEIGHT_FROM:'身高下限', PERSON_HEIGHT_TO:'身高上限',
    NECK_CIRCUMFERENCE_FROM:'颈围下限', NECK_CIRCUMFERENCE_TO:'颈围上限'
  };
  // label, chest, waist, hip, height, neck; every pair is a cm interval.
  const men = [
    ['XS',[82,88],[68,74],[84,90],[160,170],[34,36]],
    ['S',[88,94],[74,80],[90,96],[165,175],[36,38]],
    ['M',[94,100],[80,86],[96,102],[170,180],[38,40]],
    ['L',[100,106],[86,92],[102,108],[175,185],[40,42]],
    ['XL',[106,114],[92,100],[108,116],[175,190],[42,44]],
    ['2XL',[114,122],[100,108],[116,124],[175,195],[44,46]],
    ['3XL',[122,130],[108,116],[124,132],[175,195],[46,48]]
  ];
  const women = [
    ['XS',[76,82],[58,64],[82,88],[150,160],[30,32]],
    ['S',[82,88],[64,70],[88,94],[155,165],[31,33]],
    ['M',[88,94],[70,76],[94,100],[160,170],[32,34]],
    ['L',[94,100],[76,82],[100,106],[165,175],[33,35]],
    ['XL',[100,108],[82,90],[106,114],[165,180],[34,36]],
    ['2XL',[108,116],[90,98],[114,122],[165,180],[35,37]],
    ['3XL',[116,124],[98,106],[122,130],[165,180],[36,38]]
  ];
  const menPants = [
    ['28',[82,88],[69,74],[86,92],[160,175],[34,36]],
    ['30',[88,94],[74,79],[92,98],[165,180],[36,38]],
    ['32',[94,100],[79,84],[98,104],[170,185],[38,40]],
    ['34',[100,106],[84,89],[104,110],[170,190],[40,42]],
    ['36',[106,114],[89,94],[110,116],[170,195],[42,44]],
    ['38',[114,122],[94,99],[116,122],[170,195],[44,46]],
    ['40',[122,130],[99,104],[122,128],[170,195],[46,48]]
  ];
  const womenPants = [
    ['26',[76,82],[63,68],[86,92],[150,165],[30,32]],
    ['28',[82,88],[68,73],[92,98],[155,170],[31,33]],
    ['30',[88,94],[73,78],[98,104],[160,175],[32,34]],
    ['32',[94,100],[78,83],[104,110],[160,180],[33,35]],
    ['34',[100,108],[83,88],[110,116],[160,180],[34,36]],
    ['36',[108,116],[88,93],[116,122],[160,180],[35,37]],
    ['38',[116,124],[93,98],[122,128],[160,180],[36,38]]
  ];
  const boys = [
    ['2 years',[50,54],[48,52],[52,56],[86,98],[24,26]],
    ['4 years',[54,58],[50,54],[56,60],[98,110],[25,27]],
    ['6 years',[58,62],[52,56],[60,66],[110,122],[26,28]],
    ['8 years',[62,68],[54,60],[66,72],[122,134],[27,29]],
    ['10 years',[68,74],[58,64],[72,78],[134,146],[28,30]],
    ['12 years',[74,82],[62,70],[78,86],[146,158],[29,32]]
  ];
  const girls = [
    ['2 years',[50,54],[48,52],[52,56],[86,98],[24,26]],
    ['4 years',[54,58],[50,54],[56,62],[98,110],[25,27]],
    ['6 years',[58,62],[52,56],[62,68],[110,122],[26,28]],
    ['8 years',[62,68],[54,60],[68,74],[122,134],[27,29]],
    ['10 years',[68,76],[58,64],[74,82],[134,146],[28,30]],
    ['12 years',[76,84],[62,70],[82,90],[146,158],[29,31]]
  ];
  const genders = {men:['男装','339666','Man'],women:['女装','339665','Woman'],boys:['男童','339667','Boys'],girls:['女童','339668','Girls']};
  const measures = ['CHEST_CIRCUMFERENCE','WAIST_CIRCUMFERENCE','HIP_CIRCUMFERENCE','PERSON_HEIGHT','NECK_CIRCUMFERENCE'];
  const templates = [];
  const textAttr = (id, name) => ({id,values:[{name}]});
  const numberAttr = (id, number) => ({id,values:[{name:`${number} cm`,struct:{number,unit:'cm'}}]});
  function add(key, title, domain, gender, profile, system='alpha', bottom=false) {
    const [group,genderId,genderName] = genders[gender];
    const id=`builtin_v1_${gender}_${key}`, english=`${genderName} ${domain.replaceAll('_',' ')} Reference`;
    const rows=profile.map(([size,...ranges]) => ({attributes:[
      textAttr('SIZE',size), textAttr(bottom && system==='waist' ? 'PANTS_FILTRABLE_SIZES' : 'FILTRABLE_SIZE',size),
      ...measures.flatMap((field,index) => bottom && (index===0 || index===4) ? [] : [numberAttr(`${field}_FROM`,ranges[index][0]),numberAttr(`${field}_TO`,ranges[index][1])])
    ]}));
    templates.push({id,group,title:`${group} · ${title}`,system:system==='alpha' ? '字母尺码 XS–3XL' : system==='waist' ? '数字腰码' : '年龄标签 2–12 岁',
      description:bottom ? '腰围、臀围与身高参考范围；全部为身体围度，不是裤子平铺宽度。' : '胸围、腰围、臀围、身高与颈围参考范围；全部为身体尺寸，不是成衣尺寸。',
      template:{name:`${group}${title}参考尺码表`,domain_id:domain,measure_type:'BODY_MEASURE',
        names:Object.fromEntries(['CBT','MLM','MLB','MLC','MCO','MLA','MLU','MPE','MEC'].map(site => [site,english])),
        attributes:[{id:'GENDER',values:[{id:genderId,name:genderName}]},textAttr('BRAND','Generic')],
        main_attribute:[{site_id:'CBT',id:'SIZE'}],rows,
        standard_template:{preset_id:id,size_system:system}}
    });
  }
  for (const [gender,profile,pants] of [['men',men,menPants],['women',women,womenPants]]) {
    add('tshirts','T 恤','T_SHIRTS',gender,profile);
    add('shirts','衬衫','SHIRTS',gender,profile);
    add('pants','长裤／牛仔裤','PANTS',gender,pants,'waist',true);
    add('shorts','短裤','SHORTS',gender,pants,'waist',true);
    add('coats','夹克／外套','JACKETS_AND_COATS',gender,profile);
    add('hoodies','卫衣','SWEATSHIRTS_AND_HOODIES',gender,profile);
    add('pajamas','睡衣','PAJAMAS',gender,profile);
  }
  add('dresses','连衣裙','DRESSES','women',women);
  add('blouses','女式上衣','BLOUSES','women',women);
  add('leggings','打底裤','LEGGINGS','women',women,'alpha',true);
  for (const [gender,profile] of [['boys',boys],['girls',girls]]) {
    add('tshirts','T 恤','T_SHIRTS',gender,profile,'age');
    add('pants','长裤','PANTS',gender,profile,'age',true);
  }
  function freeze(value) { if (value && typeof value==='object') { Object.values(value).forEach(freeze); Object.freeze(value); } return value; }
  const byId=new Map(templates.map(item => [item.id,item]));
  return freeze({version,note,fields,templates,get:id => byId.get(id) || null});
});
