// charts.js —— 文献阅读整理 Agent 架构演示报告图表
(function() {
  var style = getComputedStyle(document.documentElement);
  var accent  = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var ink     = style.getPropertyValue('--ink').trim();
  var muted   = style.getPropertyValue('--muted').trim();
  var rule    = style.getPropertyValue('--rule').trim();
  var bg2     = style.getPropertyValue('--bg2').trim();

  // ===== 图1：五层架构调用方向 =====
  var chart1 = echarts.init(document.getElementById('chart-arch'), null, {renderer:'svg'});
  chart1.setOption({
    animation:false,
    tooltip:{appendToBody:true, trigger:'axis'},
    grid:{left:100, right:40, top:20, bottom:30},
    xAxis:{
      type:'category',
      data:['界面展示层','业务逻辑层','Agent智能体层','工具解析层','数据持久层'],
      axisLine:{lineStyle:{color:rule}},
      axisLabel:{color:ink, fontSize:13}
    },
    yAxis:{type:'value', show:false, max:5},
    series:[{
      type:'bar', data:[5,4,3,2,1], barWidth:42,
      itemStyle:{
        color:function(p){
          var palette=[accent, accent2, '#52C41A', '#FAAD14', '#8C8C8C'];
          return palette[p.dataIndex];
        },
        borderRadius:[6,6,0,0]
      },
      label:{
        show:true, position:'top',
        formatter:function(p){ return ['PyQt5','Business','LangGraph','Pure Tools','SQLite DAO'][p.dataIndex]; },
        color:ink, fontSize:13, fontWeight:600
      }
    }]
  });
  window.addEventListener('resize', function(){ chart1.resize(); });

  // ===== 图2：AI 主导解析进度时间线 =====
  var chart2 = echarts.init(document.getElementById('chart-flow'), null, {renderer:'svg'});
  chart2.setOption({
    animation:false,
    tooltip:{appendToBody:true, trigger:'axis', axisPointer:{type:'shadow'}},
    grid:{left:140, right:40, top:20, bottom:30},
    xAxis:{
      type:'value', min:0, max:100,
      axisLine:{lineStyle:{color:rule}}, splitLine:{lineStyle:{color:rule+'55'}},
      axisLabel:{color:muted, formatter:'{value}%'}
    },
    yAxis:{
      type:'category', inverse:true,
      data:['Finish','Verify','Refine补救','信任旁路校验','Director单步补救','条件本地尾部','原件通读+FC循环','版式体检','Plan准备'],
      axisLine:{lineStyle:{color:rule}}, axisTick:{show:false},
      axisLabel:{color:ink, fontSize:12}
    },
    series:[{
      type:'bar', stack:'a', barWidth:16,
      data:[[90,10],[76,16],[85,4],[80,1],[72,4],[65,7],[12,53],[8,2],[0,8]],
      itemStyle:{color:accent, borderRadius:[0,3,3,0]},
      label:{show:true, position:'right', formatter:function(p){ return p.value[1]+'%'; }, color:muted, fontSize:11}
    }]
  });
  window.addEventListener('resize', function(){ chart2.resize(); });

  // ===== 图3：六维度默认解析权重 =====
  var chart3 = echarts.init(document.getElementById('chart-dims'), null, {renderer:'svg'});
  chart3.setOption({
    animation:false,
    tooltip:{appendToBody:true},
    legend:{orient:'vertical', right:10, top:'center', textStyle:{color:ink}},
    series:[{
      name:'默认权重', type:'pie', radius:['45%','75%'], center:['40%','50%'],
      label:{color:ink, formatter:function(p){ return p.name + ' ' + (p.percent||0).toFixed(0) + '%'; }},
      data:[
        {value:1.0, name:'研究背景',  itemStyle:{color:accent}},
        {value:1.2, name:'核心观点',  itemStyle:{color:accent2}},
        {value:1.0, name:'研究方法',  itemStyle:{color:'#4096BB'}},
        {value:1.5, name:'创新点',    itemStyle:{color:'#FAAD14'}},
        {value:1.5, name:'研究结论',  itemStyle:{color:'#52C41A'}},
        {value:1.0, name:'参考文献',  itemStyle:{color:'#8C8C8C'}}
      ]
    }]
  });
  window.addEventListener('resize', function(){ chart3.resize(); });

  // ===== 图4：9 张表索引数量 =====
  var chart4 = echarts.init(document.getElementById('chart-db'), null, {renderer:'svg'});
  chart4.setOption({
    animation:false,
    tooltip:{appendToBody:true, trigger:'axis'},
    grid:{left:30, right:30, top:20, bottom:80},
    xAxis:{
      type:'category', interval:0, rotate:30, fontSize:11,
      data:['literature_info','literature_report','literature_note','category_tag','literature_tag','parse_rule','system_config','operation_log','backup_record'],
      axisLine:{lineStyle:{color:rule}}, axisLabel:{color:ink}
    },
    yAxis:{type:'value', show:false},
    series:[{
      type:'bar', barWidth:28, data:[11,7,9,10,11,5,8,8,6],
      itemStyle:{
        color:new echarts.graphic.LinearGradient(0,0,0,1,[
          {offset:0, color:accent},{offset:1, color:accent2}
        ]),
        borderRadius:[4,4,0,0]
      },
      label:{show:true, position:'top', color:ink, fontSize:11}
    }]
  });
  window.addEventListener('resize', function(){ chart4.resize(); });

  // ===== 图5：技术栈雷达 =====
  var chart5 = echarts.init(document.getElementById('chart-stack'), null, {renderer:'svg'});
  chart5.setOption({
    animation:false,
    tooltip:{appendToBody:true},
    radar:{
      indicator:[
        {name:'PyQt5 UI', max:5}, {name:'LangGraph Agent', max:5},
        {name:'SQLite 存储', max:5}, {name:'OpenAI 协议', max:5},
        {name:'PDF 处理', max:5}, {name:'反幻觉校验', max:5}
      ],
      axisName:{color:ink},
      splitLine:{lineStyle:{color:rule}},
      splitArea:{areaStyle:{color:[bg2,'transparent']}},
      axisLine:{lineStyle:{color:rule}}
    },
    series:[{
      type:'radar',
      data:[
        {value:[5,5,5,5,5,5], name:'本项目',   areaStyle:{color:accent+'40'},  itemStyle:{color:accent}},
        {value:[3,3,4,3,4,2], name:'典型同类', areaStyle:{color:accent2+'40'}, itemStyle:{color:accent2}}
      ]
    }],
    legend:{bottom:0, textStyle:{color:ink}}
  });
  window.addEventListener('resize', function(){ chart5.resize(); });
})();
