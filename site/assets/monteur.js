/* ═══════════════════════════════════════════════════════════
   MONTEUR — shared behaviours (2026)
   Every block is guarded by presence, so each page runs only
   what it actually contains. No external dependencies.
   ═══════════════════════════════════════════════════════════ */
(function(){
  "use strict";
  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ——— film grain (generated once) ——— */
  (function(){
    var n=document.createElement("canvas");n.width=n.height=120;var g=n.getContext("2d");
    var img=g.createImageData(120,120),d=img.data;
    for(var i=0;i<d.length;i+=4){var v=(Math.random()*255)|0;d[i]=d[i+1]=d[i+2]=v;d[i+3]=255;}
    g.putImageData(img,0,0);
    document.documentElement.style.setProperty("--grain","url("+n.toDataURL()+")");
  })();

  /* ——— nav: stuck state + mobile menu ——— */
  var top=document.querySelector(".top");
  var onScroll=function(){ if(top) top.classList.toggle("stuck", window.scrollY>40); };
  onScroll(); window.addEventListener("scroll",onScroll,{passive:true});
  var burger=document.querySelector(".burger"), mnav=document.querySelector(".mobile-nav");
  if(burger&&mnav){
    burger.addEventListener("click",function(){ mnav.classList.toggle("open"); document.body.style.overflow=mnav.classList.contains("open")?"hidden":""; });
    mnav.querySelectorAll("a").forEach(function(a){a.addEventListener("click",function(){mnav.classList.remove("open");document.body.style.overflow="";});});
  }

  /* ——— scroll reveal (content must never stay permanently invisible) ——— */
  if(reduced || !("IntersectionObserver" in window)){
    document.querySelectorAll(".rise").forEach(function(el){ el.classList.add("in"); });
  }else{
    var revealInView=function(){
      var vh=window.innerHeight||800, any=false;
      document.querySelectorAll(".rise:not(.in)").forEach(function(el){
        var r=el.getBoundingClientRect();
        if(r.top < vh*0.94 && r.bottom > 0){ el.classList.add("in"); any=true; }
      });
      return any;
    };
    var io=new IntersectionObserver(function(es){
      es.forEach(function(e){ if(e.isIntersecting){ e.target.classList.add("in"); io.unobserve(e.target);} });
    },{threshold:.12,rootMargin:"0px 0px -6% 0px"});
    document.querySelectorAll(".rise").forEach(function(el){ io.observe(el); });
    /* scroll backup + first paint + failsafe — belt and suspenders so nothing stays hidden */
    window.addEventListener("scroll",revealInView,{passive:true});
    window.addEventListener("resize",revealInView,{passive:true});
    requestAnimationFrame(revealInView);
    setTimeout(revealInView,600);
    setTimeout(function(){ document.querySelectorAll(".rise:not(.in)").forEach(function(el){
      if(el.getBoundingClientRect().top < (window.innerHeight||800)*1.2) el.classList.add("in"); }); },2400);
  }

  /* ——— headline kinetic reveal ——— */
  var hl=document.getElementById("headline");
  if(hl){
    hl.querySelectorAll(".w").forEach(function(w,i){w.style.animationDelay=(0.15+i*0.11)+"s";});
    requestAnimationFrame(function(){hl.classList.add("go");});
  }

  /* ——— pricing: billing toggle ——— */
  var toggle=document.querySelector(".billing-toggle");
  if(toggle){
    var btns=toggle.querySelectorAll("button");
    btns.forEach(function(b){
      b.addEventListener("click",function(){
        btns.forEach(function(x){x.classList.remove("on");});
        b.classList.add("on");
        var mode=b.getAttribute("data-mode");
        document.querySelectorAll("[data-m],[data-y]").forEach(function(el){
          var val=el.getAttribute(mode==="year"?"data-y":"data-m");
          if(val!=null) el.textContent=val;
        });
      });
    });
  }

  /* ——— download: waitlist (client-side acknowledgement) ——— */
  var wl=document.querySelector(".waitlist");
  if(wl){
    wl.addEventListener("submit",function(e){
      e.preventDefault();
      var inp=wl.querySelector("input"), note=document.querySelector(".waitlist-note");
      if(inp&&inp.value&&inp.value.indexOf("@")>0){
        if(note){note.textContent="Danke — du stehst auf der Liste. Wir melden uns zum Early-Access-Start.";note.style.color="var(--good)";}
        inp.value=""; inp.setAttribute("disabled","");
      }else{
        if(note){note.textContent="Bitte eine gültige E-Mail-Adresse eingeben.";note.style.color="var(--ember)";}
      }
    });
  }

  /* ═══ WebGL shader hero background (home) ═══ */
  (function(){
    var cv=document.getElementById("shader"); if(!cv) return;
    var gl=null; try{ gl=cv.getContext("webgl")||cv.getContext("experimental-webgl"); }catch(e){}
    if(!gl || reduced){ cv.style.background="radial-gradient(120% 90% at 75% 20%,rgba(255,154,60,.14),transparent 55%),linear-gradient(180deg,#0b0d14,#06070b)"; return; }
    var vs="attribute vec2 p;void main(){gl_Position=vec4(p,0.,1.);}";
    var fs=["precision highp float;","uniform vec2 u_res;uniform float u_time;uniform vec2 u_mouse;",
      "float hash(vec2 p){p=fract(p*vec2(123.34,456.21));p+=dot(p,p+34.56);return fract(p.x*p.y);}",
      "float noise(vec2 p){vec2 i=floor(p),f=fract(p);f=f*f*(3.-2.*f);",
        "float a=hash(i),b=hash(i+vec2(1,0)),c=hash(i+vec2(0,1)),d=hash(i+vec2(1,1));",
        "return mix(mix(a,b,f.x),mix(c,d,f.x),f.y);}",
      "float fbm(vec2 p){float v=0.,a=.5;for(int i=0;i<5;i++){v+=a*noise(p);p*=2.03;a*=.5;}return v;}",
      "void main(){vec2 uv=gl_FragCoord.xy/u_res.xy;vec2 p=uv;p.x*=u_res.x/u_res.y;",
        "float t=u_time*0.045;",
        "vec2 q=vec2(fbm(p*1.4+t),fbm(p*1.4+vec2(5.2,1.3)-t));",
        "vec2 r=vec2(fbm(p*1.4+q*1.6+vec2(1.7,9.2)+t*0.6),fbm(p*1.4+q*1.6+vec2(8.3,2.8)));",
        "float f=fbm(p*1.4+r*1.3);",
        "vec3 col=mix(vec3(0.028,0.033,0.05),vec3(0.05,0.06,0.093),f);",
        "float ember=smoothstep(0.52,0.98,f+r.x*0.45);col+=vec3(1.0,0.52,0.16)*ember*0.30;",
        "col+=vec3(0.20,0.62,0.60)*smoothstep(0.75,1.0,q.y)*0.05;",
        "float md=distance(uv,u_mouse);col+=vec3(1.0,0.6,0.22)*smoothstep(0.45,0.0,md)*0.10;",
        "float vig=distance(uv,vec2(0.5));col*=1.0-0.85*vig*vig;",
        "gl_FragColor=vec4(col,1.0);}"].join("\n");
    function sh(t,s){var o=gl.createShader(t);gl.shaderSource(o,s);gl.compileShader(o);return gl.getShaderParameter(o,gl.COMPILE_STATUS)?o:null;}
    var vsh=sh(gl.VERTEX_SHADER,vs),fsh=sh(gl.FRAGMENT_SHADER,fs);
    if(!vsh||!fsh){cv.style.background="radial-gradient(120% 90% at 75% 20%,rgba(255,154,60,.14),transparent 55%),#06070b";return;}
    var pr=gl.createProgram();gl.attachShader(pr,vsh);gl.attachShader(pr,fsh);gl.linkProgram(pr);gl.useProgram(pr);
    var buf=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buf);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,3,-1,-1,3]),gl.STATIC_DRAW);
    var loc=gl.getAttribLocation(pr,"p");gl.enableVertexAttribArray(loc);gl.vertexAttribPointer(loc,2,gl.FLOAT,false,0,0);
    var uRes=gl.getUniformLocation(pr,"u_res"),uTime=gl.getUniformLocation(pr,"u_time"),uMouse=gl.getUniformLocation(pr,"u_mouse");
    var mouse=[0.75,0.8],mt=[0.75,0.8],dpr=Math.min(window.devicePixelRatio||1,1.6);
    function resize(){var w=cv.clientWidth,h=cv.clientHeight;cv.width=w*dpr;cv.height=h*dpr;gl.viewport(0,0,cv.width,cv.height);}
    resize();window.addEventListener("resize",resize);
    window.addEventListener("pointermove",function(e){var r=cv.getBoundingClientRect();if(e.clientY<r.bottom){mt[0]=(e.clientX-r.left)/r.width;mt[1]=1-(e.clientY-r.top)/r.height;}},{passive:true});
    var t0=null,vis=true;document.addEventListener("visibilitychange",function(){vis=!document.hidden;});
    requestAnimationFrame(function frame(ts){if(t0==null)t0=ts;if(vis){mouse[0]+=(mt[0]-mouse[0])*0.05;mouse[1]+=(mt[1]-mouse[1])*0.05;gl.uniform2f(uRes,cv.width,cv.height);gl.uniform1f(uTime,(ts-t0)/1000);gl.uniform2f(uMouse,mouse[0],mouse[1]);gl.drawArrays(gl.TRIANGLES,0,3);}requestAnimationFrame(frame);});
  })();

  /* ═══ coincidence instrument (home) ═══ */
  (function(){
    var c=document.getElementById("coincidence"); if(!c) return;
    var ctx=c.getContext("2d"),W,H,dpr;
    function size(){dpr=Math.min(window.devicePixelRatio||1,2);W=c.clientWidth;H=c.clientHeight;c.width=W*dpr;c.height=H*dpr;ctx.setTransform(dpr,0,0,dpr,0,0);}
    size();window.addEventListener("resize",size);
    var EMBER="#FF9A3C",beats=8,t=0,snapEvery=2.7,tcEl=document.getElementById("tc");
    function beatX(i){var pad=Math.max(52,W*0.06);return pad+(W-2*pad)*(i/(beats-1));}
    function easeOut(x){return 1-Math.pow(1-x,3);}
    function pad2(n){return(n<10?"0":"")+n;}
    function draw(){
      ctx.clearRect(0,0,W,H);var midY=H*0.60;
      for(var i=0;i<beats;i++){var x=beatX(i);ctx.strokeStyle="rgba(255,255,255,.05)";ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(x,26);ctx.lineTo(x,H-42);ctx.stroke();ctx.fillStyle="rgba(90,215,205,.55)";ctx.beginPath();ctx.moveTo(x,H-36);ctx.lineTo(x-5,H-26);ctx.lineTo(x+5,H-26);ctx.closePath();ctx.fill();}
      ctx.strokeStyle="rgba(90,215,205,.4)";ctx.lineWidth=2;ctx.beginPath();
      for(var px=40;px<=W-40;px+=4){var ph=(px/W)*Math.PI*10+t*1.4;var amp=10+7*Math.sin(px/60);var y=H-60+Math.sin(ph)*amp*0.5;if(px===40)ctx.moveTo(px,y);else ctx.lineTo(px,y);}
      ctx.stroke();
      var cyc=(t%snapEvery)/snapEvery,tb=2+(Math.floor(t/snapEvery)%(beats-4)),toX=beatX(tb);
      var driftX=toX+(1-easeOut(Math.min(cyc/0.55,1)))*((tb%2?1:-1)*130),peakX=cyc<0.55?driftX:toX,snapped=cyc>=0.52&&cyc<0.74;
      var grad=ctx.createRadialGradient(peakX,midY-74,4,peakX,midY-74,130);grad.addColorStop(0,"rgba(255,154,60,"+(snapped?0.52:0.22)+")");grad.addColorStop(1,"rgba(255,154,60,0)");ctx.fillStyle=grad;ctx.fillRect(peakX-130,midY-200,260,250);
      ctx.strokeStyle=EMBER;ctx.lineWidth=2.5;ctx.beginPath();var sX=44,eX=W-44;
      for(var xx=sX;xx<=eX;xx+=3){var d=(xx-peakX)/82;var env=Math.exp(-d*d);var y2=midY-env*128-6;if(xx===sX)ctx.moveTo(xx,y2);else ctx.lineTo(xx,y2);}ctx.stroke();
      var crestY=midY-128-6;
      if(cyc>=0.52){ctx.strokeStyle="rgba(255,154,60,"+(snapped?0.9:0.38)+")";ctx.setLineDash([4,5]);ctx.lineWidth=1.5;ctx.beginPath();ctx.moveTo(peakX,crestY);ctx.lineTo(peakX,H-26);ctx.stroke();ctx.setLineDash([]);}
      if(snapped){var fr=(cyc-0.52)/0.22;ctx.strokeStyle="rgba(255,154,60,"+(1-fr)+")";ctx.lineWidth=2;ctx.beginPath();ctx.arc(peakX,H-26,6+fr*24,0,Math.PI*2);ctx.stroke();}
      ctx.fillStyle=EMBER;ctx.save();ctx.translate(peakX,crestY);ctx.rotate(Math.PI/4);var s=snapped?10:7.5;ctx.fillRect(-s/2,-s/2,s,s);ctx.restore();
      if(tcEl){var frames=Math.floor((t*24)%24),secs=Math.floor(t)%60,mins=Math.floor(t/60)%60;tcEl.textContent="00:"+pad2(mins)+":"+pad2(secs)+":"+pad2(frames);}
    }
    if(reduced){t=2*snapEvery+snapEvery*0.62;draw();}
    else{var last=null;requestAnimationFrame(function loop(ts){if(last==null)last=ts;t+=(ts-last)/1000;last=ts;draw();requestAnimationFrame(loop);});}
  })();

  /* ═══ bento tile micro-visuals (home / features) ═══ */
  (function(){
    var EMBER="#FF9A3C",PULSE="#5AD7CD",GRID="rgba(255,255,255,.06)",tiles=[];
    document.querySelectorAll(".tile[data-viz]").forEach(function(tile){
      var cv=tile.querySelector("canvas");if(!cv)return;var g=cv.getContext("2d");
      function fit(){var d=Math.min(window.devicePixelRatio||1,2);var w=cv.clientWidth,h=cv.clientHeight;cv.width=w*d;cv.height=h*d;g.setTransform(d,0,0,d,0,0);cv._w=w;cv._h=h;}
      fit();var o={tile:tile,cv:cv,g:g,fit:fit,kind:tile.getAttribute("data-viz"),vis:false,hover:false};
      tile.addEventListener("pointerenter",function(){o.hover=true;});
      tile.addEventListener("pointerleave",function(){o.hover=false;});
      tiles.push(o);
    });
    if(!tiles.length) return;
    window.addEventListener("resize",function(){tiles.forEach(function(o){o.fit();});});
    var tio=new IntersectionObserver(function(es){es.forEach(function(e){var o=tiles.filter(function(x){return x.tile===e.target;})[0];if(o)o.vis=e.isIntersecting;});},{threshold:.05});
    tiles.forEach(function(o){tio.observe(o.tile);});
    function renderTile(o,t){
      var g=o.g,w=o.cv._w,h=o.cv._h,k=o.kind,midY=h*0.5;g.clearRect(0,0,w,h);var sp=o.hover?1.8:1;
      if(k==="peak"){
        var cols=6;for(var i=0;i<cols;i++){var x=w*0.12+i*(w*0.76/(cols-1));g.strokeStyle=GRID;g.beginPath();g.moveTo(x,h*0.14);g.lineTo(x,h*0.8);g.stroke();g.fillStyle="rgba(90,215,205,.5)";g.beginPath();g.moveTo(x,h*0.83);g.lineTo(x-4,h*0.88);g.lineTo(x+4,h*0.88);g.closePath();g.fill();}
        var lo=w*0.12+2*(w*0.76/(cols-1)),hiX=w*0.12+3*(w*0.76/(cols-1)),ph=Math.sin(t*1.2*sp),lock=ph>0.82,px=lock?hiX:lo+(hiX-lo)*(0.5+0.5*ph);
        var gr=g.createRadialGradient(px,midY-30,2,px,midY-30,80);gr.addColorStop(0,"rgba(255,154,60,"+(lock?.5:.25)+")");gr.addColorStop(1,"rgba(255,154,60,0)");g.fillStyle=gr;g.fillRect(px-80,0,160,h);
        g.strokeStyle=EMBER;g.lineWidth=2.4;g.beginPath();for(var xx=w*0.06;xx<w*0.94;xx+=3){var d=(xx-px)/(w*0.09);var e=Math.exp(-d*d);g.lineTo(xx,midY-e*(h*0.28));}g.stroke();
        g.fillStyle=EMBER;g.save();g.translate(px,midY-h*0.28);g.rotate(Math.PI/4);var s=lock?11:8;g.fillRect(-s/2,-s/2,s,s);g.restore();
        if(lock){var fr=(ph-0.82)/0.18;g.strokeStyle="rgba(255,154,60,"+(1-fr)+")";g.lineWidth=2;g.beginPath();g.arc(px,h*0.86,4+fr*20,0,7);g.stroke();}
      }else if(k==="silence"){
        var gs=w*0.42,ge=w*0.60;g.fillStyle="rgba(255,154,60,.09)";g.fillRect(gs,h*0.12,ge-gs,h*0.76);
        g.strokeStyle="rgba(255,154,60,.35)";g.setLineDash([3,4]);g.beginPath();g.moveTo(gs,h*0.12);g.lineTo(gs,h*0.88);g.moveTo(ge,h*0.12);g.lineTo(ge,h*0.88);g.stroke();g.setLineDash([]);
        g.strokeStyle=PULSE;g.lineWidth=2;for(var b=0;b<2;b++){g.globalAlpha=b?0.3:1;g.beginPath();for(var x2=w*0.06;x2<w*0.94;x2+=3){var inG=x2>gs&&x2<ge;var amp=inG?1.5:(h*0.13+h*0.08*Math.sin(x2/13+t*2*sp));var y=midY+Math.sin(x2/7+t*3*sp+b)*amp*(inG?0.15:0.5);if(x2<=w*0.06)g.moveTo(x2,y);else g.lineTo(x2,y);}g.stroke();}g.globalAlpha=1;
      }else if(k==="drop"){
        var heights=[.34,.54,.4,.92,.48,.6,.44],n=heights.length,pad=w*0.1,bw=(w-2*pad)/n,strong=3,pl=0.5+0.5*Math.sin(t*3*sp);
        for(var i2=0;i2<n;i2++){var bx=pad+i2*bw+bw*0.18,bh=(h*0.66)*heights[i2],on=i2===strong;g.fillStyle=on?EMBER:"rgba(90,215,205,.26)";if(on)g.globalAlpha=0.55+0.45*pl;g.fillRect(bx,h*0.84-bh,bw*0.62,bh);g.globalAlpha=1;
          if(on){g.fillStyle="rgba(255,154,60,.9)";g.beginPath();var cx=bx+bw*0.31,cy=h*0.84-bh-10;for(var a=0;a<5;a++){var ang=-Math.PI/2+a*2*Math.PI/5;g.lineTo(cx+Math.cos(ang)*5,cy+Math.sin(ang)*5);var ang2=ang+Math.PI/5;g.lineTo(cx+Math.cos(ang2)*2,cy+Math.sin(ang2)*2);}g.closePath();g.fill();}}
      }else if(k==="loop"){
        var y=midY-6;g.strokeStyle=GRID;g.lineWidth=2;g.beginPath();g.moveTo(w*0.1,y);g.lineTo(w*0.9,y);g.stroke();g.fillStyle=PULSE;g.beginPath();g.arc(w*0.1,y,4,0,7);g.fill();
        var hp=w*0.1+(w*0.8)*((t*0.28*sp)%1);g.fillStyle=EMBER;g.save();g.translate(hp,y);g.rotate(Math.PI/4);g.fillRect(-4.5,-4.5,9,9);g.restore();
        g.strokeStyle="rgba(255,154,60,.6)";g.lineWidth=2;g.beginPath();g.moveTo(w*0.9,y);g.bezierCurveTo(w*0.9,y+h*0.3,w*0.1,y+h*0.3,w*0.1,y);g.stroke();
        g.fillStyle="rgba(255,154,60,.6)";g.beginPath();g.moveTo(w*0.1,y);g.lineTo(w*0.1+6,y+7);g.lineTo(w*0.1-6,y+7);g.closePath();g.fill();
      }else if(k==="mix"){
        g.strokeStyle="rgba(90,215,205,.5)";g.lineWidth=2;g.beginPath();var ax=w*0.5;for(var x3=w*0.08;x3<w*0.92;x3+=3){var dd=(x3-ax)/(w*0.12);var duck=1-0.55*Math.exp(-dd*dd);var y=midY+h*0.18-h*0.16*duck;if(x3<=w*0.08)g.moveTo(x3,y);else g.lineTo(x3,y);}g.stroke();
        var spk=0.6+0.4*Math.abs(Math.sin(t*2*sp));g.strokeStyle=EMBER;g.lineWidth=2.5;g.beginPath();g.moveTo(ax,midY+h*0.1);g.lineTo(ax,midY+h*0.1-h*0.34*spk);g.stroke();g.fillStyle=EMBER;g.beginPath();g.arc(ax,midY+h*0.1-h*0.34*spk,4,0,7);g.fill();
      }else if(k==="hygiene"){
        var xs=w*0.08,gap=5,blocks=[.16,.22,.19,.26,.15],y0=midY-h*0.14,hh=h*0.28;
        for(var i3=0;i3<blocks.length;i3++){var bwid=w*blocks[i3];if(xs+bwid>w*0.92)break;g.fillStyle="rgba(90,215,205,.26)";g.fillRect(xs,y0,bwid,hh);g.strokeStyle="rgba(90,215,205,.5)";g.strokeRect(xs+.5,y0+.5,bwid,hh);xs+=bwid+gap;}
        var f=Math.abs(Math.sin(t*1.6*sp));g.globalAlpha=f*0.7;g.fillStyle=EMBER;g.fillRect(w*0.5,y0,3,hh);g.globalAlpha=1;g.strokeStyle="rgba(255,154,60,.5)";g.setLineDash([3,3]);g.beginPath();g.moveTo(w*0.5+3,y0+hh/2);g.lineTo(w*0.5+20,y0+hh/2);g.stroke();g.setLineDash([]);
      }
    }
    if(reduced){tiles.forEach(function(o){renderTile(o,1.2);});}
    else{var s0=null;requestAnimationFrame(function loop(ts){if(s0==null)s0=ts;var t=(ts-s0)/1000;tiles.forEach(function(o){if(o.vis)renderTile(o,t);});requestAnimationFrame(loop);});}
  })();
})();
