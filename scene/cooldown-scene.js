/* cooldown-scene.js: a Red Line window onto the Charles and Back Bay, in pixels.
No dependencies, no network, no clock: the page picks the look.
const s = CooldownScene.mount(canvas, {season:'spring', time:'dusk'});
s.setLook({season:'autumn', time:'night'});  s.stop();
CooldownScene.lookFor(new Date())  ->  {season, time}   (Boston: season by month, dusk around sunset)
mount options: still (one frame, no movement), maxScale (cap the zoom), maxHeight (px or function, cap the zoom by height), container (element whose width to fit).
The canvas is 240x168 native and is shown at the largest whole multiple (in device pixels) that fits its parent's width. */
(function(G){'use strict';
const W=240,H=168,WY0=40,WY1=132,LW=[16,112],RW=[128,224],HZ=108,WR=23;
const SQ=['#c8423b','#3f8f4a','#d8b13a','#3a6fc0','#7a4fa0','#d06a2d'];
const BCN=[59,76],ANT=[185,71];
const SEASONS=['spring','summer','autumn','winter'],TIMES=['day','dusk','night'];
const mk=(w,h)=>{const k=document.createElement('canvas');k.width=w||W;k.height=h||H;return k;};
/* position hash: lit windows, stars and tree colours stay put across looks */
const hsh=(a,b)=>{let h=Math.imul(a|0,374761393)^Math.imul(b|0,668265263);h=Math.imul(h^(h>>>13),1274126177);return((h^(h>>>16))>>>0)/4294967296;};
const B4=[0,8,2,10,12,4,14,6,3,11,1,9,15,7,13,5],dith=(X,Y)=>(B4[(Y&3)*4+(X&3)]+.5)/16;
const pen=k=>{const g=k.getContext('2d');g.imageSmoothingEnabled=false;return{g,R:(col,X,Y,w,h)=>{g.fillStyle=col;g.fillRect(Math.round(X),Math.round(Y),Math.max(1,Math.round(w)),Math.max(1,Math.round(h)));},P:(col,X,Y)=>{g.fillStyle=col;g.fillRect(Math.round(X),Math.round(Y),1,1);}};};
const rgb=h=>[1,3,5].map(i=>parseInt(h.slice(i,i+2),16));
const hex=a=>'#'+a.map(v=>Math.round(Math.max(0,Math.min(255,v))).toString(16).padStart(2,'0')).join('');
const mix=(a,b,k)=>{const A=rgb(a),Z=rgb(b);return hex(A.map((v,i)=>v+(Z[i]-v)*k));};
/* hand-made caps for the ad cards (no G or M); N and W are 5 wide so they can't read as M or H */
const FONT={B:['110','101','110','101','110'],R:['110','101','110','101','101'],S:['011','100','010','001','110'],Y:['101','101','010','010','010'],A:['010','101','111','101','101'],C:['011','100','100','100','011'],D:['110','101','101','101','110'],E:['111','100','110','100','111'],F:['111','100','110','100','100'],H:['101','101','111','101','101'],I:['111','010','010','010','111'],K:['101','101','110','101','101'],L:['100','100','100','100','111'],N:['10001','11001','10101','10011','10001'],O:['111','101','101','101','111'],P:['111','101','111','100','100'],T:['111','010','010','010','010'],U:['101','101','101','101','111'],W:['10001','10001','10101','10101','01010']};
function text(p,str,X,Y,col,s){s=s||1;for(const ch of str){const g=FONT[ch];if(!g){X+=3*s;continue;}g.forEach((r,j)=>{for(let i=0;i<r.length;i++)if(r[i]==='1')p.R(col,X+i*s,Y+j*s,s,s);});X+=(g[0].length+1)*s;}}
const inset=(y,y0,y1,r)=>{const d=Math.min(y-y0,y1-1-y);return d>=r?0:Math.round(r-Math.sqrt(r*r-(r-d)*(r-d)));};
function rrect(p,x0,y0,x1,y1,r,col){for(let y=y0;y<y1;y++){const i=inset(y,y0,y1,r);p.R(col,x0+i,y,x1-x0-2*i,1);}}

/* looks: sky ramps [warm, winter] per time; autumn is derived from the warm ramp */
const LOOKS={
day:{sky:[['#5db5ea','#6bbdec','#79c5ee','#88cdf0','#98d5f2','#a9dcf3','#bbe4f5','#cdebf6'],['#7aaedd','#86b6e1','#93bfe5','#a1c8e9','#b0d1ec','#bfdaef','#cde2f2','#dae9f4']],tint:['#000000','#000000'],amt:0,haze:['#cdebf6','#dae9f4'],hz:.5,lit:0,glow:0},
dusk:{sky:[['#26285a','#302f65','#3d3670','#4f3e7a','#664783','#814f88','#9e5889','#bb6286','#d47081','#e5847a','#ef9b73','#f4b46f','#f6c97c'],['#1f2a58','#283464','#334072','#424c7e','#565889','#6e6491','#887097','#a27c9a','#bc889a','#d29696','#e2a993','#ecbf98','#f2d4a6']],tint:['#3c3566','#323e6c'],amt:.52,haze:['#cf8389','#b9939f'],hz:.3,lit:.36,glow:.25},
night:{sky:[['#0d1130','#101536','#13193c','#171e43','#1c234a','#212850','#282d56','#30325a','#3a375d','#463c5e','#52425e'],['#1c1b2b','#211f31','#272436','#2e293b','#362e40','#3f3445','#493a49','#53404d','#5d4650','#664c53','#6e5256']],tint:['#13172f','#16141f'],amt:.7,haze:['#43395a','#5a4650'],hz:.22,lit:.58,glow:0}
};
/* autumn: deeper top, amber horizon, same ramp steps */
const AUTUMN={day:['#2a6fb8','#f2d9a8',.2,.5],dusk:['#1c1a4a','#f59a4a',.25,.5],night:['#2a1f4f','#2a1f4f',.15,0]};
function skyFor(time,season){const T=LOOKS[time],WI=season==='winter',n=T.sky[WI?1:0].length;let sky=T.sky[WI?1:0],haze=T.haze[WI?1:0];
if(season==='autumn'){const[top,hor,a,b]=AUTUMN[time];sky=sky.map((c,i)=>{const u=i/(n-1);return mix(mix(c,top,(1-u)*a),hor,Math.max(0,u-.5)*b);});haze=mix(haze,'#e8c08a',.3);}
return{sky,tint:T.tint[WI?1:0],amt:T.amt,haze,hz:T.hz,lit:T.lit,glow:T.glow};}
const WARM=['#ffd27a','#ffc25e','#f6dfa6','#ffb553'],COOL=['#e4ecf7','#cbd8ee'];
const BEACON={spring:{on:'#5a9bff',off:'#28406e',flash:7},summer:{on:'#3d8bff'},autumn:{on:'#4f8dff'},winter:{on:'#ff3b3b',off:'#7a3a3a',flash:7}};
/* Esplanade foliage by material ramp: [light, mid, shade] */
const TREES={spring:[['#b3d97a','#86bd58','#56913f']],summer:[['#78b456','#4a8a3c','#2f6a32']],
autumn:[['#f0c05a','#d8892f','#a24d2c'],['#e8a03a','#c8622b','#8f3a2b'],['#d9603c','#a8412f','#7a2f2d'],['#f2d76e','#d9a63e','#a8702f'],['#a9b857','#6f8c3f','#48692f']]};
const FALL={spring:{day:['#f9cdd9','#f0a6bd'],dusk:['#f6bfcb','#e19aaf'],night:['#d6a6bb','#ad8aa2']},autumn:{day:['#e8a03a','#c4622b','#e8c25a'],dusk:['#e29a48','#b8583a','#d9b050'],night:['#a8783a','#7e4a38','#9a8444']}};
const PN={day:16,dusk:12,night:8};
/* inside the car: warmer and dimmer at the edges once it's dark out */
const FG={
day:{wall:'#b9bdc1',ceil:'#3b3936',cl:'#55514b',glow:['#ef9f48','#f5b357','#f9c36a','#fac86f','#f6b85c','#f0a54d'],fr:'#8e9296',c1:'#f3f3ef',c2:'#1d6f8a',ln:'#a3a39f',bar:'#e3e6ea',bs:'#8d939a',br:'#6d7279',wo:'#d0d4d8',gk:'#33373d',pl:'#c6cacd',pll:'#a3a7ab',sl:'#aeb2b6',sll:'#8f9398',seat:'#1b2231',st:'#2b3446',sg:'#0d1118',strap:'#2c3038',sh:'#4a4f58'},
dusk:{wall:'#c0b4a4',ceil:'#3a322b',cl:'#5c5045',glow:['#f2a24a','#f8b95b','#fccd74','#fdd27c','#f9c066','#f2aa50'],fr:'#968a7a',c1:'#f6ecd8',c2:'#206b80',ln:'#b4aa94',bar:'#eee2ca',bs:'#958877',br:'#75695d',wo:'#d4c7b3',gk:'#302b2a',pl:'#c8bba7',pll:'#a69885',sl:'#ad9e8b',sll:'#8d7f6f',seat:'#1e1f2d',st:'#30314a',sg:'#0f1018',strap:'#2e2a2a',sh:'#4f4842'},
night:{wall:'#b6a68e',ceil:'#342c25',cl:'#5a4c3f',glow:['#f6ae55','#fcc46a','#ffd98c','#ffe09a','#fdcb78','#f6b35c'],fr:'#8f8270',c1:'#f8edd3',c2:'#21667a',ln:'#b8ab92',bar:'#f0e2c4',bs:'#8f8170',br:'#706354',wo:'#cbbba2',gk:'#2b2725',pl:'#bfae95',pll:'#9b8b74',sl:'#a49379',sll:'#84745f',seat:'#1d1d2b',st:'#2f2f47',sg:'#0e0e16',strap:'#2b2726',sh:'#4d453e'}
};
const ICE={day:['#dfe8f0','#cfdce8','#bccddd','#aabdd0'],dusk:['#d9c3d0','#c3b0c4','#ab9bb8','#968aa9'],night:['#6f6480','#5f566f','#50495f','#433d52']};
const CRACK={day:'#93a8be',dusk:'#7f7499',night:'#383247'},DRIFT={day:['#f6f9fc','#e2ebf3'],dusk:['#f1dfe3','#cdb9cb'],night:['#8d819a','#6c6280']};

/* a rough Boston sunset, so the page can ask what it looks like out now */
function lookFor(d){d=d||new Date();const m=d.getMonth(),season=m>=2&&m<=4?'spring':m>=5&&m<=7?'summer':m>=8&&m<=10?'autumn':'winter',
n=Math.floor((Date.UTC(d.getFullYear(),m,d.getDate())-Date.UTC(d.getFullYear(),0,0))/864e5),g=2*Math.PI/365*(n-1),
eq=229.18*(.000075+.001868*Math.cos(g)-.032077*Math.sin(g)-.014615*Math.cos(2*g)-.040849*Math.sin(2*g)),
dc=.006918-.399912*Math.cos(g)+.070257*Math.sin(g)-.006758*Math.cos(2*g)+.000907*Math.sin(2*g)-.002697*Math.cos(3*g)+.00148*Math.sin(3*g),
la=42.36*Math.PI/180,ha=Math.acos(Math.cos(1.5855)/(Math.cos(la)*Math.cos(dc))-Math.tan(la)*Math.tan(dc))*180/Math.PI,
loc=u=>u-d.getTimezoneOffset(),rise=loc(720+284.24-4*ha-eq),set=loc(720+284.24+4*ha-eq),now=d.getHours()*60+d.getMinutes();
return{season,time:now<rise+15||now>=set+40?'night':now>=set-35?'dusk':'day'};}

function mount(canvas,opts){opts=opts||{};
const x=canvas.getContext('2d',{alpha:false});x.imageSmoothingEnabled=false;
canvas.width=W;canvas.height=H;canvas.setAttribute('role','img');
canvas.style.display='block';canvas.style.imageRendering='pixelated';if(canvas.style.imageRendering!=='pixelated')canvas.style.imageRendering='crisp-edges';
const far=mk(),fg=mk(),rail=mk(32,11),isle=mk(),tex=mk(W*2,WR),floe=mk(W*2,WR);
let seed=1;const rnd=()=>((seed=(seed*16807)%2147483647)/2147483647);
let season='spring',time='dusk',L,GL=[],WB=[],farD,texD,floeD;

const tone=(col,k,d)=>{let o=L.amt?mix(col,L.tint,Math.min(1,L.amt*(k==null?1:k))):col;return d?mix(o,L.haze,d*L.hz):o;};
const lit=(xx,yy,cool,f)=>{if(!L.lit||hsh(xx,yy)>=L.lit*(f||1))return null;const h=hsh(yy+101,xx+57);return cool&&h<.45?COOL[h<.2?0:1]:WARM[Math.floor(hsh(xx+7,yy+3)*4)];};

/* ---------- outside: sky, Back Bay, the Esplanade (static) ---------- */
function buildFar(){const p=pen(far),day=time==='day',dusk=time==='dusk',night=time==='night',WI=season==='winter',SP=season==='spring',AU=season==='autumn',S=!WI;
seed=7;GL=[];p.g.clearRect(0,0,W,H);
/* dithered sky, with the afterglow sitting low in the west (right) */
const st=L.sky,n=st.length;
for(let y=WY0;y<HZ;y++)for(let X=0;X<W;X++){let t=(y-WY0)/(HZ-WY0);
if(L.glow){const dx=(X-206)/80,dy=(HZ+4-y)/40;t+=L.glow*Math.exp(-dx*dx-dy*dy);}
const v=Math.max(0,Math.min(n-1,t*(n-1)));let i=Math.floor(v);if(v-i>dith(X,y))i++;p.P(st[Math.min(n-1,i)],X,y);}
if(night&&S)for(let i=0;i<16;i++)p.P(hsh(i,13)<.35?'#d4d7ef':'#6f76a8',Math.floor(hsh(i,11)*W),WY0+2+Math.floor(hsh(i,12)*13));
/* a waxing moon in the west: crescent at dusk, first quarter later */
const MOON=S&&dusk?['..###..','....##.','.....##','.....##','.....##','....##.','..###..']:S&&night?['...##..','...###.','...####','...####','...####','...###.','...##..']:null;
if(MOON){const mx=196,my=45,mc=dusk?'#f8ecd2':'#f3ead0';
if(night)for(let j=-4;j<11;j++)for(let i=-4;i<11;i++){const d=Math.hypot(i-3,j-3);if(d>3.6&&d<7.5&&(1-(d-3.6)/3.9)*.75>dith(mx+i,my+j))p.P('#232a55',mx+i,my+j);}
MOON.forEach((r,j)=>{for(let i=0;i<7;i++)if(r[i]==='#')p.P(mc,mx+i,my+j);});GL.push({x:mx+5,c:'#efe6cc',c2:'#9d9aa6',p:.45});}
/* clouds */
const cloud=(cx,cy,w,top,mid,low)=>{p.R(top,cx+3,cy,w-7,1);p.R(top,cx+1,cy+1,w-3,1);p.R(mid,cx,cy+2,w,1);p.R(low,cx+2,cy+3,w-4,1);};
if(day&&S)for(const[cx,cy,w]of[[196,92,22],[214,96,16],[140,97,14]]){p.R('#eef6fc',cx,cy,w,2);p.R('#ffffff',cx+3,cy-1,w-6,1);p.R('#e2eef8',cx+2,cy+2,w-3,1);}
if(dusk&&SP)for(const[cx,cy,w]of[[24,54,30],[60,66,18],[136,50,40],[184,62,30],[204,78,20]]){const k=Math.max(0,Math.min(1,(cx-90)/110));cloud(cx,cy,w,'#6c5a8c',mix('#a66c92','#d58a8c',k),mix('#ee9d8a','#f8c886',k));}
if(dusk&&season==='summer')for(const[cx,cy,w]of[[130,88,32],[184,78,42]]){p.R('#c98292',cx+4,cy,w-8,1);p.R('#f2b07c',cx,cy+1,w,1);p.R('#f7c886',cx+3,cy+2,w-6,1);}
if(dusk&&AU)for(const[cx,cy,w]of[[14,60,34],[84,72,22],[128,56,44],[190,70,36],[206,86,16]]){const k=Math.max(0,Math.min(1,(cx-80)/120));cloud(cx,cy,w,'#5f4f86',mix('#9c6a8e','#d4805a',k),mix('#ee9468','#f8bd6c',k));}
if(dusk&&WI)for(const[cx,cy,w]of[[18,48,46],[100,60,24],[140,44,56],[198,58,28],[206,78,18]]){const k=Math.max(0,Math.min(1,(cx-90)/110));cloud(cx,cy,w,'#3e4672',mix('#8a7fa4','#c39aa6',k),mix('#d9a9b0','#f0c3a6',k));}
const snow=(X,w,Y)=>{if(WI)p.R(tone('#f4f7fb',.5,.25),X,Y,w,1);};
const rim=(X,top,h,base)=>{if(dusk)p.R(mix(tone(base,1,.45),WI?'#e7b3a6':'#f3a684',.45),X,top,1,h);};
const blk=(X,w,top,col,win,d)=>{p.R(tone(col,1,d),X,top,w,HZ-top);const wc=tone(win||'#6c6f78',1,d);
for(let yy=top+2;yy<HZ-2;yy+=2)for(let xx=X+1;xx<X+w-1;xx+=2)if(rnd()<.55)p.P(lit(xx,yy,1)||wc,xx,yy);snow(X,w,top);};
/* far blocks: South End, Copley, Fenway */
[[0,10,95,'#c9c2b6'],[10,8,97,'#b7aea2'],[100,8,93,'#c8b9a3'],[106,6,95,'#b29f8a'],[224,8,95,'#bfae98'],[232,8,93,'#ad9c88']].forEach(([X,w,t,col])=>blk(X,w,t,col,0,1));
/* landmarks placed and sized from the photo (3.5 photo px per art px); lit faces left, cool glass right */
const D=.45,D2=.4;
[[16,6,95,'#c9c2b6'],[22,7,97,'#b7aea2'],[45,8,95,'#b8aca0'],[66,28,91,'#c0bfc2'],[94,7,93,'#d5c0b0'],[101,6,96,'#c8a898'],[107,6,95,'#b8ada3'],[128,7,96,'#b8735a'],[134,7,94,'#c8bca9'],[210,6,97,'#b8a48e'],[217,7,96,'#c0b3a3']].forEach(([X,w,t,c])=>blk(X,w,t,c,0,.6));
[[28,13,84,'#c5b8aa','#8a7f7a',.55],[38,8,92,'#d9906f','#7a4a3c',.5],[65,9,84,'#d7b8a2','#8a6c66',.5],[72,5,82,'#c3ccd8','#6c7a8c',.5],[76,4,85,'#cfc7bd',0,.5],
[142,10,89,'#b9b6ae','#7c8088',.55],[146,11,92,'#e0946f','#7a4a3c',.4],[155,6,90,'#6f9fc4','#4a6a8c',.45],[162,9,90,'#d3c4ae','#8a7a68',.4],[178,8,92,'#d3c5b0',0,.45],[194,9,92,'#9fb6cc','#5f7a96',.45],[199,9,98,'#e0946f','#7a4a3c',.3],[203,5,95,'#cfcfc9',0,.4]].forEach(([X,w,t,c,wn,d])=>blk(X,w,t,c,wn,d));
/* the Berkeley Building: salmon brick lit on the left, mauve shade, a dark slate truncated pyramid with a pale sign band, a spike of a beacon mast */
const bx=53,bw=tone('#7b6c78',1,D);
p.R(tone('#d99a82',1,D),bx,83,4,25);p.R(tone('#b8a0a4',1,D),bx+4,83,5,25);p.R(tone('#8f8590',1,D),bx+9,83,2,25);
for(let yy=85;yy<106;yy+=2)for(let xx=bx+1;xx<bx+9;xx+=2)p.P(lit(xx,yy)||bw,xx,yy);
const rf=tone('#52729a',1,D*.6);p.R(rf,bx+1,82,9,1);p.R(rf,bx+2,81,7,1);p.R(rf,bx+3,80,5,1);p.R(day?'#dfe6ef':'#c7b98f',bx+4,81,3,1);
const ms=tone('#b8c6d4',1,D*.5);for(let yy=BCN[1]+1;yy<80;yy++)p.P(ms,BCN[0],yy);
rim(bx+11,83,25,'#b8a0a4');snow(bx+1,9,82);snow(bx+3,5,80);
/* the Hancock: pale glass on the lit face, a saturated blue one beside it, two thin masts */
const HX=78,HW=16,HT=63,HH=HZ-HT;
const HKD=WI?['#6f7aa8','#8382aa','#9a8aab','#b495aa','#cba2a6','#ddb0a2','#e9c1a2']:['#7d6fa8','#9474a4','#ad7a9e','#c88497','#de9290','#eda587','#f4b97e'];
const HKN=WI?['#2b2738','#322c3f','#272333','#38304a','#2e2a3c','#252131']:['#1c2444','#212a4c','#19203d','#252e52','#1e2647','#18203c'];
const hb=S?['#a6dbe6','#b3e1ec','#9bd3e2','#bde6ef','#a6dbe6','#92cbdc']:['#c3dcee','#cfe3f2','#b8d5ea','#d8e8f4','#c3dcee','#aecfe6'];
for(let X=HX;X<HX+HW;X++)for(let yy=HT;yy<HZ;yy++){const bd=Math.floor((yy-HT)/6+(X-HX)*.15),rt=X>=HX+10;let col;
if(dusk){const v=Math.max(0,Math.min(6,(yy-HT)/HH*6+(bd%2?.6:-.4)));let i=Math.floor(v);if(v-i>dith(X,yy))i++;col=HKD[Math.min(6,i)];if(rt)col=mix(col,'#2a2250',.3);}
else if(night){col=HKN[bd%6];if(rt)col=mix(col,'#05070f',.3);}
else{col=hb[bd%6];if(rt)col=mix(col,'#2f86c0',.55);}
if((yy-HT)%3===0)col=day?mix(col,'#3e6fa4',.3):mix(col,night?'#0c1024':'#3b2f5a',.35);
p.P(col,X,yy);}
if(night)for(let yy=HT+3;yy<HZ;yy+=3)for(let X=HX+1;X<HX+HW-1;X++)if(hsh(X,yy)<.09)p.P(hsh(yy,X)<.5?'#b9c6e0':'#e8cf98',X,yy);
const hs=day?['#d8f2f6','#4f86b8']:dusk?['#f0c7b4','#6a5282']:['#343f6a','#10152b'];
for(let yy=HT;yy<HZ;yy++){p.P(hs[0],HX,yy);p.P(hs[1],HX+9,yy);}p.R(hs[0],HX,HT,HW,1);
const an=tone('#8d939c',1,.4);p.R(an,HX+8,HT-3,1,3);p.R(an,HX+10,HT-2,1,2);snow(HX,HW,HT);
/* 111 Huntington: only the faceted glass dome clears the roofs in front */
const dm=day?(S?['#9cc6e0','#e8eef2']:['#c5d9ee','#e0ecf7']):['#cfe0f7','#f4f8ff'];
[[170,3],[169,5],[169,5],[168,7],[168,7],[168,7],[168,7],[168,7]].forEach(([X,w],j)=>{p.R(dm[0],X,84+j,w,1);p.P(dm[1],X,84+j);});p.R(dm[1],170,84,2,1);
p.R(tone('#8fb8d8',1,D2),168,92,7,16);rim(175,86,22,'#d9dde3');
/* the Pru: cream on the lit face, blue glass beside it, a white roof overhang and a thin mast; One Dalton's blue slab just behind */
p.R(tone('#3c6f9a',1,D2),190,78,4,30);p.R(tone('#7db4d5',1,D2),190,78,4,30);p.R(tone('#a9cfe6',1,D2),191,78,1,30);p.R(tone('#5f8fb4',1,D2),193,78,1,30);
if(L.lit)for(let yy=80;yy<106;yy+=2)for(let xx=190;xx<194;xx++){const l=lit(xx,yy,0,.8);if(l)p.P(l,xx,yy);}
p.R(tone('#e2ddd0',1,D2),180,76,5,32);p.R(tone('#7ea8c8',1,D2),185,76,5,32);
for(let yy=78;yy<106;yy+=2){p.R(tone('#d2cdc0',1,D2),180,yy,5,1);}for(let yy=77;yy<106;yy+=7)p.R(tone('#4f7396',1,D2),185,yy,5,1);
for(let yy=80;yy<106;yy+=2)for(let xx=181;xx<190;xx++){const l=lit(xx,yy,1,1.1);if(l)p.P(l,xx,yy);}
p.R(tone('#eef0ec',1,D2),179,75,12,1);p.R(tone('#8c97a4',1,D2),180,76,10,1);if(!day)p.R('#ffd991',181,76,8,1);
const pm=tone('#8d939c',1,D2);for(let yy=ANT[1]+1;yy<75;yy++)p.P(pm,ANT[0],yy);p.P(pm,182,73);p.P(pm,182,74);rim(190,76,32,'#a9cfe6');snow(180,10,75);
[[150,20,95,'#b7a89a','#6c5a52',.35],[168,8,93,'#aab4c0',0,.45],[175,4,96,'#9aa7b6',0,.5]].forEach(([X,w,t,c,wn,d])=>blk(X,w,t,c,wn,d));
/* Beacon Street's river side */
const brick=['#a2563f','#b0634a','#8f4a38','#c07a5a','#9c5a46'],dk=day?'#e9e0d2':tone('#3d3542',.7);
for(let X=0;X<W;){const w=5+Math.floor(rnd()*4),top=97+Math.floor(rnd()*3),col=brick[Math.floor(rnd()*5)];
p.R(tone(col,1,.12),X,top,w,HZ-top);p.R(WI?tone('#f4f7fb',.5,.2):tone('#5a3b33',1,.12),X,top-1,w,1);
for(let xx=X+1;xx<X+w-1;xx+=2)for(let yy=top+2;yy<104;yy+=3)p.P(lit(xx,yy,0,1.1)||dk,xx,yy);X+=w;}
/* Esplanade trees: three flat bands, no dither; winter is bare forks over a snowbank */
for(let X=0;X<W;X+=2+Math.floor(rnd()*3)){const h=5+Math.floor(rnd()*5),y0=HZ-h;
if(S){const pal=TREES[season][Math.floor(hsh(X,9)*TREES[season].length)];
for(let k=0;k<h;k++){const ww=Math.max(2,Math.round(4*Math.sin(Math.PI*(k+1)/(h+1))));p.R(tone(pal[k<2?0:(k<h-2?1:2)],AU?.65:1),X-ww/2,y0+k,ww,1);}}
else if(hsh(X,3)<.4){const br=tone('#4a3f3c'),t0=HZ-4-Math.min(h,7);
for(let yy=t0+2;yy<HZ-1;yy++)p.P(br,X,yy);p.P(br,X,t0+1);p.P(br,X-1,t0+1);p.P(br,X+1,t0+1);p.P(br,X-2,t0);p.P(br,X+2,t0);}}
if(WI){p.R(tone('#f4f7fb',.5,.2),0,HZ-2,W,1);p.R(tone('#d3dbe6',.7,.2),0,HZ-1,W,1);}
/* the lagoon bank's big rounded trees (left window), three flat bands like the rest */
if(S)for(const[cx,h,w]of[[64,10,9],[73,8,8],[82,11,10],[91,9,9]]){const pal=TREES[season][Math.floor(hsh(cx,9)*TREES[season].length)];
for(let k=0;k<h;k++){const ww=Math.max(3,Math.round(w*Math.sin(Math.PI*(k+1.5)/(h+2))));p.R(tone(pal[k<3?0:(k<h-3?1:2)],AU?.65:1),cx-ww/2,HZ-h+k,ww,1);}
p.R(tone('#4a3a34'),cx,HZ-2,1,2);}
/* a few cherry trees in bloom, spring only */
if(SP){const CB=['#fde3ec','#f7b8cb','#ea94ae','#c76f8e'].map((c2,i)=>tone(c2,.5+i*.08)),tk=tone('#5a3d3a');
for(const cx of[10,57,103,141,183,213]){const ty=HZ-8;
['...###...','.#######.','#########','#########','.#######.'].forEach((r,j)=>{for(let i=0;i<9;i++)if(r[i]==='#'||(j&&j<4&&hsh(cx+i,j+50)<.3)){const s=j*.85+(i-4)*.22+(hsh(cx+i,j)-.5)*1.3;p.P(CB[s<1?0:s<2.1?1:s<3.2?2:3],cx-4+i,ty+j);}});
p.P(tk,cx-1,ty+5);p.P(tk,cx+1,ty+5);p.P(tk,cx,ty+6);p.P(tk,cx,ty+7);p.P(CB[1],cx-5,HZ-1);p.P(CB[2],cx+5,HZ-1);p.P(CB[1],cx+7,HZ-1);}}
/* the Hatch Shell from behind, Community Boating's masts in front */
const dc=S?['#e2bfae','#cfa491','#b08a82']:['#e8dad6','#d3c3c4','#b7aab4'];
for(let j=0;j<7;j++){const hw=Math.round(6.5*Math.sqrt(1-((6-j)/7)**2));for(let X=36-hw;X<=36+hw;X++){const u=(X-(36-hw))/(2*hw+1);p.P(WI&&j<2?tone('#ffffff',.5,.2):tone(dc[u<.35?0:u<.7?1:2],.9,.2),X,99+j);}}
p.R(tone('#e7e9ec'),43,106,17,1);p.R(tone('#c7cbd1'),43,107,17,1);
if(S){const mc=tone('#f2f2ef',.8);[44,47,50,53,56,59].forEach((xx,i)=>{const h=[5,4,6,4,5,4][i];for(let yy=106-h;yy<106;yy++)p.P(mc,xx,yy);if(h>4)p.P(mc,xx+1,106-h+2);});}
/* Esplanade lamps once the light goes */
if(!day){const lp=tone('#2a2830');
for(const lx of[50,70,90,132,152,170,196,221]){if(night){p.P('#9c7652',lx-1,103);p.P('#9c7652',lx+1,103);}p.P('#ffe3a0',lx,103);for(let yy=104;yy<HZ;yy++)p.P(lp,lx,yy);if(!WI)GL.push({x:lx,c:'#ffd88c',c2:'#b48a58',p:night?.45:.3});}
if(!WI)GL.push({x:BCN[0],b:1,p:.3});}
p.R(tone(WI?'#a9b6c8':AU?'#6b6250':'#5e6b62'),0,HZ,W,1);}

/* the river: ripple texture twice the width; winter is a quiet ice sheet */
/* the lagoon's grass spit, a thin strip lying in the water */
function buildIsle(){const p=pen(isle);p.g.clearRect(0,0,W,H);
const c=season==='winter'?['#eef3f8','#c5d0de']:season==='autumn'?['#a8914f','#7a6a3a']:['#7da35f','#5f8a4c'];
p.R(tone(c[0],.6),36,110,43,1);p.R(tone(c[0],.6),31,111,51,1);p.R(tone(c[1],.6),36,112,43,1);}
function buildTex(){const p=pen(tex),q=pen(floe),WI=season==='winter';seed=21;p.g.clearRect(0,0,W*2,WR);q.g.clearRect(0,0,W*2,WR);
if(WI){const cr=CRACK[time],dr=DRIFT[time];
for(let b=0;b<4;b++){const r0=b*6,rows=b<3?6:5;
for(let i=0;i<2;i++)q.R(cr,Math.floor(rnd()*(W*2-30)),r0+Math.floor(rnd()*rows),9+Math.floor(rnd()*(8+b*4)),1);
const w=7+b*3+Math.floor(rnd()*5),X=Math.floor(rnd()*(W*2-w-2)),yy=r0+1+Math.floor(rnd()*(rows-3));q.R(dr[0],X,yy,w,1);q.R(dr[1],X+1,yy+1,w-2,1);}
return;}
const WT={day:['#86b0e0','#7aa6da','#6e9bd3','#a4c7ec','#5f8ac6'],dusk:['#8a5a88','#5e4a80','#3a3468','#d99486','#2c2852'],night:['#1e2244','#171b38','#11152d','#343a66','#0c0f24']}[time];
WB=Array.from({length:WR},(_,r)=>rgb(mix(WT[0],WT[2],Math.min(4,Math.floor(r/5))/4)));
for(let yy=0;yy<WR;yy++)for(let X=Math.floor(rnd()*8);X<W*2;X+=9+Math.floor(rnd()*14)){const len=1+Math.floor(rnd()*(2+yy/4));p.R(rnd()<.55?WT[3]:WT[4],X,yy,len,1);}}

/* ---------- the Longfellow's railing: one 48 px tile ---------- */
const RH=11;
function buildRail(){const p=pen(rail),WI=season==='winter';p.g.clearRect(0,0,32,RH);
const[hi,mid,lo]={day:['#eadbb9','#d6c49e','#b5a27b'],dusk:['#c9b29f','#a8927f','#7f6b60'],night:['#d0ac7c','#a2825f','#6e5845']}[time];
p.R(hi,0,0,32,1);p.R(mid,0,1,32,1);p.R(lo,0,2,32,1);p.R(mid,0,9,32,1);p.R(lo,0,10,32,1);
p.R(mid,0,3,32,1);for(let X=4;X<32;X+=4){p.R(hi,X,4,1,5);p.R(mid,X+1,4,1,5);p.P(lo,X+2,6);}
p.R(mid,0,0,4,11);p.R(hi,0,0,1,11);p.R(lo,3,2,1,9);p.R(hi,0,0,4,1);
if(WI){const sw=time==='day'?'#ffffff':time==='dusk'?'#efdcdf':'#c9c0cc';p.R(sw,0,0,32,1);p.P(sw,1,1);}}

/* ---------- inside the car ---------- */
function buildFg(){const p=pen(fg),F=FG[time];seed=3;p.g.clearRect(0,0,W,H);
p.R(F.wall,0,0,W,H);
p.R(F.ceil,0,0,W,8);for(const[x0,x1]of[[3,117],[123,237]])F.glow.forEach((col,i)=>p.R(col,x0,1+i,x1-x0,1));p.R(F.cl,0,7,W,1);
for(const[x0,x1,bgc]of[[7,112,F.c1],[128,233,F.c2]]){p.R(F.fr,x0-1,9,x1-x0+2,22);p.R(bgc,x0,10,x1-x0,20);}
/* made-up Boston ads: a cannoli (pistachio ends, sugar dust) and a baseball */
const cannoli=(X,Y)=>{for(let i=0;i<15;i++){const yc=Y+3-(i/5|0),cap=i<3||i>11;
for(let j=0;j<6;j++)p.P(cap?(j%5?'#e8d9bd':'#fffaf0'):['#efc98a','#e0aa63','#d29a52','#c58a45','#a8692f','#8f5726'][j],X+i,yc+j);}
for(const[i,j]of[[0,2],[1,3],[2,1],[12,2],[13,1],[14,3]])p.P('#5a3520',X+i,Y+3-(i/5|0)+j);
for(const[i,j]of[[5,2],[8,3],[10,2]])p.P('#9a5d2a',X+i,Y+3-(i/5|0)+j);for(const i of[4,7,9,6])p.P('#ffffff',X+i,Y+2-(i/5|0));};
const ball=(X,Y)=>{[5,7,9,9,11,11,11,9,9,7,5].forEach((w,j)=>{for(let i=0;i<w;i++)p.P(i>w-3||j>8?'#d9d4c4':'#f6f3ea',X+5-(w>>1)+i,Y+j);});
for(const[i,j]of[[3,2],[2,3],[2,4],[2,6],[2,7],[3,8],[7,2],[8,3],[8,4],[8,6],[8,7],[7,8]])p.P('#c0392b',X+i,Y+j);};
cannoli(9,13);text(p,'FRESH',26,13,'#1b1b1b');text(p,'CANNOLI',26,20,'#c0392b');
for(const[yy,w]of[[13,40],[16,44],[19,36],[22,42],[25,30]])p.R(F.ln,62,yy,w,1);
text(p,'PLAY BALL',134,15,'#ffffff',2);p.R('#7fc4d8',134,27,72,1);ball(214,14);
p.R(F.bs,0,33,W,1);p.R(F.bar,0,32,W,1);for(const bx of[3,120,236])p.R(F.br,bx,30,2,3);
for(const[x0,x1]of[LW,RW]){rrect(p,x0-3,WY0-3,x1+3,WY1+3,10,F.wo);rrect(p,x0-2,WY0-2,x1+2,WY1+2,9,F.gk);
for(let y=WY0;y<WY1;y++){const i=inset(y,WY0,WY1,8);p.g.clearRect(x0+i,y,x1-x0-2*i,1);}}
p.R(F.pl,113,36,14,100);p.R(F.pll,120,36,1,100);
p.R(F.sl,0,136,W,5);p.R(F.sll,0,140,W,1);
for(let s=0;s<4;s++){const x0=s*60+1,x1=s*60+59;rrect(p,x0,141,x1,H+6,4,F.seat);p.R(F.st,x0+3,141,x1-x0-6,1);
for(let i=0;i<26;i++){let sx=x0+4+Math.floor(rnd()*(x1-x0-12)),sy=145+Math.floor(rnd()*20),dy=rnd()<.5?1:-1;const col=mix(F.seat,SQ[Math.floor(rnd()*6)],.2);
for(let k=0;k<4+Math.floor(rnd()*4);k++){p.P(col,sx++,sy);if(rnd()<.45){sy+=dy;if(rnd()<.4)dy=-dy;}}}
for(const sy of[143,152,160])p.R(F.st,x0+4,sy,x1-x0-8,1);}
for(const bx of[59,60,119,120,179,180])p.R(F.sg,bx,141,1,27);
for(const sx of[79,162]){p.R(F.strap,sx,33,3,17);p.R(F.sh,sx,33,1,17);for(let a=0;a<30;a++){const t=a/30*6.283,px=Math.round(sx+1+Math.cos(t)*3.5),py=Math.round(54+Math.sin(t)*3);p.P(F.strap,px,py);p.P(F.strap,px,py+(py>54?1:0));}}}

/* ---------- animation ---------- */
const flakes=Array.from({length:120},(_,i)=>({x:hsh(i,1)*W,y:WY0+hsh(i,2)*(WY1-WY0),v:.25+hsh(i,3)*.45,d:hsh(i,4)*6.28}));
const bits=Array.from({length:16},(_,i)=>({x:hsh(i,5)*W,y:WY0+hsh(i,6)*(WY1-WY0),v:.12+hsh(i,7)*.18,d:hsh(i,8)*6.28}));
const boats=[{x:60,y:113,s:1},{x:150,y:111,s:.8},{x:205,y:115,s:1.2}];
const wImg=x.createImageData(W,WR);
let railOff=0,wt=0,tick=0,still=!!opts.still,reduced=null;
/* ripples drift faster the closer they are */
function drawWater(t){const d=wImg.data,WI=season==='winter';
if(WI){const IC=ICE[time].map(rgb);
for(let r=0;r<WR;r++){const b=Math.min(3,Math.floor(r/6)),off=Math.floor(wt*(.18+b*.3)),[br,bg,bb]=IC[b];
for(let X=0;X<W;X++){const o=(r*W+X)*4,ti=(r*W*2+(X+off)%(W*2))*4,f=floeD[ti+3];
d[o]=f?floeD[ti]:br;d[o+1]=f?floeD[ti+1]:bg;d[o+2]=f?floeD[ti+2]:bb;d[o+3]=255;}}
x.putImageData(wImg,0,HZ+1);return;}
const REF=[.55,.45,.36,.27,.19,.12];
for(let r=0;r<WR;r++){const[br,bg,bb]=WB[r],off=Math.floor(wt*(.15+r*.035)),sh=Math.round(Math.sin(r*1.3+t*.003)*.9),kr=r<6?REF[r]:0;
for(let X=0;X<W;X++){const o=(r*W+X)*4,ti=(r*W*2+(X+off)%(W*2))*4,si=((HZ-4)*W+Math.min(W-1,Math.max(0,((X+sh)>>2)<<2)))*4;
let R=br+(farD[si]-br)*kr,G=bg+(farD[si+1]-bg)*kr,Bb=bb+(farD[si+2]-bb)*kr;
if(texD[ti+3]){R=(R+texD[ti])/2;G=(G+texD[ti+1])/2;Bb=(Bb+texD[ti+2])/2;}
d[o]=R;d[o+1]=G;d[o+2]=Bb;d[o+3]=255;}}
x.putImageData(wImg,0,HZ+1);}
function frame(t){if(still){t=0;}else{tick++;wt++;}
const day=time==='day',night=time==='night',WI=season==='winter',SP=season==='spring',AU=season==='autumn',S=!WI,BE=BEACON[season],bOn=!BE.flash||still||Math.floor(tick/BE.flash)%2;
x.drawImage(far,0,0);
if(night&&S)for(let i=0;i<4;i++){x.fillStyle=Math.sin(tick*.13+i*2.3)>.55?'#ffffff':'#8a90bf';x.fillRect(Math.floor(hsh(i,11)*W),WY0+2+Math.floor(hsh(i,12)*13),1,1);}
/* Berkeley beacon: steady blue clear, flashing blue clouds, flashing red snow */
const bc=bOn?BE.on:BE.off;if(!day&&bOn){x.fillStyle=mix(bc,'#1a1f45',.55);x.fillRect(BCN[0]-1,BCN[1],3,1);x.fillRect(BCN[0],BCN[1]-1,1,3);}x.fillStyle=bc;x.fillRect(BCN[0],BCN[1],1,1);
const aOn=still||Math.floor(tick/10)%2;if(!day&&aOn){x.fillStyle=mix('#ff4a4a','#1a1f45',.6);x.fillRect(ANT[0]-1,ANT[1],3,1);x.fillRect(ANT[0],ANT[1]-1,1,3);}x.fillStyle=aOn?'#ff4a4a':'#7a3a3a';x.fillRect(ANT[0],ANT[1],1,1);
drawWater(t);x.drawImage(isle,0,0);
for(const g of GL){let c1=g.c,c2=g.c2;if(g.b){if(!bOn)continue;c1=c2=BE.on;}
for(let r=0;r<WR;r++){if(hsh(g.x*3+r,(tick>>1)+g.x*7)>g.p*(1-r/WR*.6))continue;const w=1+(hsh(r+g.x,tick)<.3),jx=Math.round(Math.sin(r*1.7+tick*.3+g.x)*(.5+r*.06));
x.fillStyle=r<3?c1:c2;x.fillRect(g.x+jx-(w>>1),HZ+1+r,w,1);}}
if(S&&day&&!still)for(let i=0;i<6;i++){x.fillStyle='#e6f3fc';x.fillRect(Math.floor(Math.random()*W),HZ+3+Math.floor(Math.random()*8),1,1);}
if(S&&!night)for(const bt of boats){if(!still){bt.x-=.05*bt.s;if(bt.x<-6)bt.x=W+6;}const bx=Math.round(bt.x),by=bt.y;x.fillStyle=day?'#f7f7f4':'#f6c9a0';for(let k=0;k<5;k++)x.fillRect(bx,by-k-1,Math.max(1,Math.round((5-k)*.6)),1);x.fillStyle=day?'#d9dde3':'#3e3550';x.fillRect(bx-1,by,4,1);}
if(!still)railOff=(railOff+1.4)%32;for(let X=-32;X<W+32;X+=32)x.drawImage(rail,Math.round(X-railOff),WY1-RH);
if(WI){const fc={day:['#ffffff','#e3ebf5'],dusk:['#f6e8ec','#d8cbe0'],night:['#ece6ee','#b9b0c4']}[time];
for(const f of flakes){if(!still){f.y+=f.v;f.d+=.05;f.x+=Math.sin(f.d)*.25-.35;if(f.y>WY1){f.y=WY0;f.x=Math.random()*W;}if(f.x<0)f.x+=W;}x.fillStyle=f.v>.5?fc[0]:fc[1];x.fillRect(Math.round(f.x),Math.round(f.y),1,1);}}
/* cherry petals in spring, falling leaves in autumn: drifting left as the train moves */
const pc=FALL[season]&&FALL[season][time];
if(pc){const fall=AU?1.5:1;for(let i=0;i<PN[time];i++){const q=bits[i];
if(!still){q.d+=.09;q.x-=.42+.22*Math.sin(q.d);q.y+=fall*(q.v+.12*Math.sin(q.d*1.3));if(q.y>WY1||q.x<-2){q.y=(AU?88:WY0)+Math.random()*30;q.x=W+Math.random()*40;}}
if(AU&&!q.k){q.k=1;q.y=88+(q.y-WY0)*.48;}
x.fillStyle=pc[Math.floor((Math.sin(q.d*1.7)+1)*.4999*pc.length)];x.fillRect(Math.round(q.x),Math.round(q.y),Math.sin(q.d*2.3)>.2?2:1,1);}}
x.drawImage(fg,0,0);}

function label(){return'Pixel-art view from a Red Line train crossing the Longfellow Bridge, seen through two train windows: the Charles River, the Back Bay skyline and the bridge railing, in '+season+' '+{day:'daylight',dusk:'at dusk',night:'at night'}[time]+'. Ad cards for fresh cannoli and "Play ball".';}
function applyLook(l){l=l||{};if(SEASONS.indexOf(l.season)>=0)season=l.season;if(TIMES.indexOf(l.time)>=0)time=l.time;
L=skyFor(time,season);buildFar();buildTex();buildRail();buildFg();buildIsle();
farD=far.getContext('2d').getImageData(0,0,W,H).data;texD=tex.getContext('2d').getImageData(0,0,W*2,WR).data;floeD=floe.getContext('2d').getImageData(0,0,W*2,WR).data;
canvas.setAttribute('aria-label',label());}

/* whole-number zoom in device pixels, so every art pixel is the same size */
function fit(){const dpr=G.devicePixelRatio||1,el=opts.container||canvas.parentElement||document.body,cs=G.getComputedStyle(el);
const avail=el.clientWidth-(parseFloat(cs.paddingLeft)||0)-(parseFloat(cs.paddingRight)||0);
let s=Math.max(1,Math.floor(avail*dpr/W+1e-6)),mh=typeof opts.maxHeight==='function'?opts.maxHeight():opts.maxHeight;if(opts.maxScale)s=Math.min(s,opts.maxScale);if(mh)s=Math.min(s,Math.max(1,Math.floor(mh*dpr/H+1e-6)));
canvas.style.width=W*s/dpr+'px';canvas.style.height=H*s/dpr+'px';}

let raf=0,last=0,dead=false;
const loop=t=>{raf=requestAnimationFrame(loop);if(t-last<66)return;last=t;frame(t);};
const halt=()=>{if(raf){cancelAnimationFrame(raf);raf=0;}};
const go=()=>{if(!raf&&!still&&!dead&&!document.hidden)raf=requestAnimationFrame(loop);};
const mq=G.matchMedia&&G.matchMedia('(prefers-reduced-motion: reduce)');
const setStill=()=>{still=!!opts.still||!!(mq&&mq.matches);if(still){halt();railOff=wt=tick=0;frame(0);}else go();};
const onVis=()=>{if(document.hidden)halt();else go();};
const onMq=()=>setStill();
let ro=null;
G.addEventListener('resize',fit);document.addEventListener('visibilitychange',onVis);
if(mq&&mq.addEventListener)mq.addEventListener('change',onMq);
if(G.ResizeObserver&&(opts.container||canvas.parentElement)){ro=new G.ResizeObserver(fit);ro.observe(opts.container||canvas.parentElement);}
/* paint now: the loop draws on animation frames 66 ms apart, which a hidden tab or a headless browser may not deliver for a while */
applyLook({season:opts.season||'spring',time:opts.time||'dusk'});fit();frame(0);setStill();
return{setLook(l){applyLook(l);if(still){railOff=wt=tick=0;frame(0);}else if(!raf)go();},look:()=>({season,time}),
stop(){dead=true;halt();G.removeEventListener('resize',fit);document.removeEventListener('visibilitychange',onVis);if(mq&&mq.removeEventListener)mq.removeEventListener('change',onMq);if(ro)ro.disconnect();}};}

G.CooldownScene={mount,lookFor,SEASONS,TIMES,size:[W,H]};
})(typeof window!=='undefined'?window:this);
