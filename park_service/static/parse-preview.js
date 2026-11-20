const source=document.getElementById('parse-source');
if(source){
  source.addEventListener('input',()=>{
    document.querySelectorAll('form[action^="/parse/"] button').forEach(button=>{button.disabled=true;});
    document.getElementById('preview-invalidated').hidden=false;
  });
}
