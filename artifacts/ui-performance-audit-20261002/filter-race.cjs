// Execute the actual dashboard loader with two overlapping filter selections.
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('bit/templates/infraction_dashboard.html', 'utf8');
const fn = source.slice(source.indexOf('        async function loadData()'), source.indexOf('        async function loadSyncStatus()'));
const requests = [];
const context = vm.createContext({
  state: {loading:false}, ipRightsMode:false, filter:'first',
  elements: new Proxy({}, {get(target,key) {return target[key] ||= {value:'30'};}}),
  dashboardParams: () => new URLSearchParams({search:context.filter}),
  fetch: url => new Promise(resolve => requests.push({url,resolve})),
  renderMetrics(){}, renderGroups(){}, renderRows(){}, fillSelect(){},
  fillCategorySelect(){}, showStatus(){},
});
vm.runInContext(fn, context);
(async()=>{
  const first = context.loadData();
  context.filter = 'second';
  await context.loadData();
  requests[0].resolve({ok:true,json:async()=>({status:'success',data:{}})});
  await first;
  console.log(JSON.stringify({selectedFilter:context.filter,requestedUrls:requests.map(r=>r.url),
    latestFilterRequested:requests.some(r=>r.url.includes('second'))},null,2));
})();
