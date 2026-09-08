/* A DOM shim just large enough to execute the app's rendering code under Node.
   This is a smoke harness, not a browser: it proves the views build their trees
   against the real API without throwing, which is what static checks cannot. */
class ClassList {
  constructor(node){ this.node = node; }
  get _set(){ return new Set((this.node.className||"").split(/\s+/).filter(Boolean)); }
  _write(s){ this.node.className = [...s].join(" "); }
  add(...c){ const s=this._set; c.forEach(x=>x&&s.add(x)); this._write(s); }
  remove(...c){ const s=this._set; c.forEach(x=>s.delete(x)); this._write(s); }
  toggle(c,on){ const s=this._set; if(on===undefined?!s.has(c):on) s.add(c); else s.delete(c); this._write(s); }
  contains(c){ return this._set.has(c); }
}
class Node2 {
  constructor(tag){
    this.tagName=(tag||"").toUpperCase(); this.children=[]; this.childNodes=[];
    this.attributes={}; this.className=""; this.style={}; this.dataset={};
    this._text=""; this.classList=new ClassList(this); this.listeners={};
    this.hidden=false; this.disabled=false; this.value=""; this.options=[];
    this.parentElement=null;
  }
  setAttribute(k,v){ this.attributes[k]=String(v); if(k==="class") this.className=String(v); }
  getAttribute(k){ return this.attributes[k]; }
  removeAttribute(k){ delete this.attributes[k]; }
  addEventListener(t,f){ (this.listeners[t] ||= []).push(f); }
  append(...kids){ for(const k of kids){ if(k===null||k===undefined) continue;
    if(k && k._isFragment){ this.append(...k.childNodes); continue; }
    const n = (k instanceof Node2 || k?._isText) ? k : { _isText:true, textContent:String(k) };
    n.parentElement = this; this.childNodes.push(n); if(n instanceof Node2) this.children.push(n); } }
  replaceChildren(...kids){ this.childNodes=[]; this.children=[]; this.append(...kids); }
  get textContent(){ return this._text || this.childNodes.map(c=>c.textContent??"").join(""); }
  set textContent(v){ this._text=String(v); this.childNodes=[]; this.children=[]; }
  set innerHTML(v){ this._text=String(v).replace(/<[^>]*>/g,""); }
  get innerHTML(){ return this._text; }
  querySelector(){ return null; }
  querySelectorAll(){ return []; }
  get parentNode(){ return this.parentElement; }
}
class Frag { constructor(){ this._isFragment=true; this.childNodes=[]; }
  append(...kids){ for(const k of kids){ if(k===null||k===undefined) continue;
    if(k && k._isFragment){ this.append(...k.childNodes); continue; }
    this.childNodes.push((k instanceof Node2||k?._isText)?k:{_isText:true,textContent:String(k)}); } } }
const registry = {};
globalThis.document = {
  createElement: (t)=>new Node2(t),
  createElementNS: (_ns,t)=>new Node2(t),
  createTextNode: (t)=>({ _isText:true, textContent:String(t) }),
  createDocumentFragment: ()=>new Frag(),
  getElementById: (id)=> (registry[id] ||= new Node2("div")),
  querySelector: ()=>null,
  querySelectorAll: ()=>[],
  documentElement: new Node2("html"),
  addEventListener(){},
};
globalThis.document.documentElement.setAttribute=()=>{};
globalThis.document.documentElement.removeAttribute=()=>{};
globalThis.window = { addEventListener(){}, scrollTo(){}, location:{ hash:"#/" } };
globalThis.location = { hash:"#/", reload(){} };
globalThis.localStorage = { getItem:()=>null, setItem(){}, removeItem(){} };
globalThis.Node = Node2;
globalThis.setInterval = ()=>0;
export { registry };
