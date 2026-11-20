async function updateReminders(){
  try{
    const response=await fetch('/reminders',{headers:{Accept:'application/json'}});
    if(!response.ok)return;
    const result=await response.json();
    const banner=document.getElementById('clock-status');
    if(banner)banner.textContent=`演示时间 +${result.offset_seconds} 秒 · ${new Date(result.effective_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})}（上海时间）`;
    document.querySelectorAll('[data-ticket-timing]').forEach(node=>{
      const flag=result.flags[node.dataset.ticketTiming]; if(!flag)return;
      const labels=[];
      if(flag.phone_followup)labels.push('需要人工电话跟进（演示）：特急推送满 300 秒未响应，未自动拨号');
      if(flag.overdue)labels.push('当前超期（按人工截止时间）');
      if(flag.historical_late_closed)labels.push('历史超期关闭');
      if(flag.deadline_unconfigured)labels.push('截止时间未配置（Demo 临时假设），无法判断是否超期');
      node.textContent=labels.join(' · ');
    });
  }catch(_){/* preserve server-rendered current flags when offline */}
}
setInterval(updateReminders,15000);
