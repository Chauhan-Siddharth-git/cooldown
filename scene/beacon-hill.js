/* beacon-hill.js: a Beacon Hill lane in pixels: brick rowhouses both sides, gas lamps, cobbles between brick sidewalks.
Placed and sized from the owner's photos (the dusk one fixes the camera): a lane in that style, not a copy of one street.
The gate shows it on alternate days with cooldown-scene.js, whose interface and 240x168 canvas it shares:
mount(canvas, {season, time, still, maxScale, maxHeight, container}), setLook, look, stop; lookFor(new Date()) picks the look. */
(function(G){'use strict';
const W=240,H=168,N=W*H,SEASONS=['spring','summer','autumn','winter'],TIMES=['day','dusk','night'];
/* the camera, fitted to the dusk photo: vanishing point (161,102), eye 1.2 m up, house fronts 3.9 m to the left,
the right-hand wall 1.31 m to the right, curbs at -1.53 and +.47. Metres in, pixels out. */
const VX=161,VY=102,F=172,E=1.2,XL=-3.9,XR=1.31,CL=-1.53,CR=.47,ZX=55,ZE=62;
const sx=(X,z)=>VX+X*F/z,sy=(Y,z)=>VY+(E-Y)*F/z;
const hsh=(a,b)=>{let h=Math.imul(a|0,374761393)^Math.imul(b|0,668265263);h=Math.imul(h^(h>>>13),1274126177);return((h^(h>>>16))>>>0)/4294967296;};
const B4=[0,8,2,10,12,4,14,6,3,11,1,9,15,7,13,5],dith=(X,Y)=>(B4[(Y&3)*4+(X&3)]+.5)/16;
const rgb=h=>[1,3,5].map(i=>parseInt(h.slice(i,i+2),16));
const mix=(a,b,k)=>a.map((v,i)=>v+(b[i]-v)*k);
const cl=(v,a,b)=>v<a?a:v>b?b:v;

/* left houses: [from z, to z, height, door at the near end (1) or the far end (0), door colour, a hood over it] */
const LH=[[2,6.45,14,0,'wood',0],[6.45,12.9,13.6,1,'door',1],[12.9,18.2,13.3,1,'door',0],[18.2,23.1,12.2,0,'door',0],[23.1,28.3,13.4,1,'red',1],
[28.3,33.4,12.1,0,'door',0],[33.4,38.6,12.8,1,'wood',0],[38.6,43.5,11.6,0,'door',1],[43.5,49,12.4,1,'door',0],[49,55,11.8,0,'red',0]];
/* right side: [from, to, height, kind] 0 house, 1 a garden wall with the tree behind it */
const RH=[[2,5.2,14,0],[5.2,14,3,1],[14,20,10.5,0],[20,27,11.5,0],[27,35,10,0],[35,44,11,0],[44,55,10.5,0]];
const topOf=(L,z)=>{for(const h of L)if(z<h[1])return h[2];return 0;};
/* gas lamps, which in Boston burn day and night: [X, z, on a post (1) or a bracket off the left wall (0)] */
const LAMPS=[[.98,4.8,1],[-3.25,19,0],[.9,27,1],[-3.25,36,0],[.9,46,1],[-3.25,52,0]];
const TREE=[2.2,13.5];
/* the crown, traced from the dusk photo: [y, left x, right x] */
const CROWN=[[0,146,212],[8,152,208],[16,153,200],[24,150,190],[40,153,188],[56,158,189],[64,155,178],[80,154,176],[92,155,171],[99,159,167]];
const crownAt=y=>{for(let k=1;k<CROWN.length;k++){const[y1,l1,r1]=CROWN[k];if(y<=y1){const[y0,l0,r0]=CROWN[k-1],t=(y-y0)/(y1-y0);return[l0+(l1-l0)*t,r0+(r1-r0)*t];}}return null;};

/* materials: six anchors from cool shadow through daylight (the fourth) to warm lamp-light, each pair split, so eleven steps */
const R6=t=>t.split(' ').map(h=>'#'+h);
const MAT='brick mortar granite trim shutter glass door cobble gap walk snow iron leaf bark slate asphalt plant red pink yellow purple pumpkin wreath blue wood'.split(' ');
const K={};MAT.forEach((n,i)=>K[n]=i+1);
const RAMP={brick:R6('1e1828 3a2230 62303a a44c3a cc663e f09c5c'),mortar:R6('2c2a38 4c4450 77686a a89888 d0b896 f4d8a8'),
granite:R6('262a3a 464c5c 70747e a2a29e ccc6b6 f4e4c4'),trim:R6('2c2c3c 55546a 8e8c98 e0dcd4 f6efe0 fff6e2'),
shutter:R6('0a0c11 11141a 191d24 232830 30363a 4c4840'),glass:R6('0c1120 141d32 1e2b46 2e405c 4a6080 7a90a8'),
door:R6('0a0b10 121419 1b1e24 272b31 383b40 5a544c'),cobble:R6('1e1e28 36353e 5e5a5e 968e82 bcb09a e8d0a4'),
gap:R6('0f1017 191920 26262c 3c3a3c 5a524a 80705a'),walk:R6('241b27 432731 703c40 a85650 cc7462 f0a67c'),
snow:R6('2e3452 4c5680 7c88ac b8c2da eef0f2 ffd8a8'),iron:R6('07080b 0e1015 16191f 20242a 30343a 55504a'),
bark:R6('100e12 1a171b 282224 3a3230 504440 6e5c4e'),slate:R6('14161f 1d2029 282c36 3a3f4a 50555e 726e6a'),
asphalt:R6('15161c 1f2027 2c2d34 44454c 5c5c60 7e776a'),plant:R6('0e1c16 173020 22482a 336a32 4e9040 86c05a'),
red:R6('2c0e16 4c121c 7e1a22 b82a2a e2443a ff7a5a'),pink:R6('2e1a2c 52283f 844068 c46a98 e494b8 ffc4d6'),
yellow:R6('2a2412 4a3e14 7c6618 c4a020 ecc830 fff07a'),purple:R6('1c1428 2e1c44 4a2a6c 6e40a0 9464c4 c494e0'),
pumpkin:R6('2a1410 4c2212 803814 c45a1a ec8424 ffb44a'),wreath:R6('0b1813 11251a 1a3a24 28562f 3c723c 609652'),
blue:R6('0c1026 141a3a 1e2858 2c3c80 3e529c 6a7cb8'),wood:R6('0d1612 12201a 1a3024 264632 365c40 527252')};
const LEAF={spring:R6('152418 233e26 3a6230 5c8e3c 86b84c c0dc78'),summer:R6('0e1a14 183020 24492a 3c6e34 5e9442 9cc25a'),
autumn:R6('2a1414 4e2218 823a1c c06424 e8962e fcd060'),winter:RAMP.bark};
const R11=a=>{const p=a.map(rgb),o=[];for(let i=0;i<11;i++)o.push(i&1?mix(p[i>>1],p[(i>>1)+1],.5):p[i>>1]);return o;};
/* the look: sky (top to bottom; the winter row is snow cloud lit from below), ambient light, lamp strength, haze */
const SKY={day:[R6('8fb3d9 a3c0df b6cde5 c8d8ea d8e2ee'),R6('bcc5d0 c6ced8 d0d6de d9dee4 e2e6ea')],
dusk:[R6('7d8db4 97a3c4 b2b8d0 cbc9d8 e0d6d8'),R6('5d6a8a 76809c 9097ac aaacbb c2bfc6')],
night:[R6('080b1a 0b0f22 0f1429 141a31 1a2038'),R6('46505e 525d6a 606b76 6f7a82 7d888e')]};
const AMB={day:.7,dusk:.44,night:.1},LK={day:.08,dusk:.5,night:1},HAZE={day:['#b8c4d0',.3],dusk:['#8a88a8',.4],night:['#101426',.5]};
const WARM=['#ffd27a','#ffc25e','#f6dfa6','#ffb553'].map(rgb);

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
/* per pixel: material (0 = a colour of its own), shade offset, where it is in the lane, which way it faces
(0 up, 1 and 2 the left and right house fronts, 3 the camera, 4 any way: leaves) */
const M=new Uint8Array(N),V=new Float32Array(N),PX=new Float32Array(N),PY=new Float32Array(N),PZ=new Float32Array(N),NR=new Uint8Array(N),C=new Array(N),GW=new Float32Array(N);
const img=x.createImageData(W,H),base=new Uint8ClampedArray(N*4);
let season='summer',time='dusk',WI,AU,SP,day,dusk,night,flames=[];

const put=(c,r,m,v,X,Y,z,n)=>{if(c<0||c>=W||r<0||r>=H)return;const i=r*W+c;M[i]=m;V[i]=v||0;PX[i]=X;PY[i]=Y;PZ[i]=z;NR[i]=n;};
const own=(c,r,col,z)=>{if(c<0||c>=W||r<0||r>=H)return;const i=r*W+c;M[i]=0;C[i]=col;PZ[i]=z||0;};
/* the three kinds of flat surface, sampled at pixel centres; a sliver thinner than a pixel still gets one if it is a third of one */
const span=(a,b,f)=>{const lo=Math.min(a,b),hi=Math.max(a,b);let p0=Math.ceil(lo-.5),p1=Math.floor(hi-.5);if(p1<p0&&hi-lo>.3)p0=p1=Math.round((lo+hi)/2-.5);for(let p=p0;p<=p1;p++)f(p);};
/* X = const: a house front */
const pX=(X,z0,z1,Y0,Y1,f)=>span(sx(X,z0),sx(X,z1),c=>{const z=cl(F*X/(c+.5-VX),Math.min(z0,z1),Math.max(z0,z1));span(sy(Y1,z),sy(Y0,z),r=>f(c,r,cl(E-(r+.5-VY)*z/F,Y0,Y1),z));});
/* Y = const: the ground, a step, the underside of a hood */
const pY=(Y,X0,X1,z0,z1,f)=>span(sy(Y,z0),sy(Y,z1),r=>{const z=cl(F*(E-Y)/(r+.5-VY),Math.min(z0,z1),Math.max(z0,z1));span(sx(X0,z),sx(X1,z),c=>f(c,r,cl((c+.5-VX)*z/F,X0,X1),z));});
/* z = const: anything facing the camera */
const pZ=(z,X0,X1,Y0,Y1,f)=>span(sx(X0,z),sx(X1,z),c=>span(sy(Y1,z),sy(Y0,z),r=>f(c,r,(c+.5-VX)*z/F,E-(r+.5-VY)*z/F)));
const side=X=>X<0?1:2;
const fill=(X,z0,z1,Y0,Y1,m,v)=>pX(X,z0,z1,Y0,Y1,(c,r,Y,z)=>put(c,r,m,v,X,Y,z,side(X)));
const flat=(Y,X0,X1,z0,z1,m,v)=>pY(Y,X0,X1,z0,z1,(c,r,X,z)=>put(c,r,m,v,X,Y,z,Y>E?3:0));
const face=(z,X0,X1,Y0,Y1,m,v)=>pZ(z,X0,X1,Y0,Y1,(c,r,X,Y)=>put(c,r,m,v,X,Y,z,3));

/* brick in courses; u runs along the wall in metres, lpx is pixels per metre along it; mortar shows once a course is over two pixels */
const brick=(c,r,X,Y,z,u,hv,n,lpx,v0)=>{const ch=.075*F/z,ci=Math.floor(Y/.075),bu=(u+(ci&1?.1:0))/.2,bi=Math.floor(bu),fy=Y/.075-ci,fu=bu-bi;
const mo=ch>=2.2&&(fy<.28||(.2*lpx>=3&&fu<.12));put(c,r,mo?K.mortar:K.brick,(v0||0)+(mo?0:(hsh(bi*7+hv,ci)-.5)*.1+(hsh(bi,ci*3)<.05?-.1:0)),X,Y,z,n);};
const wall=(X,z0,z1,Y0,Y1,hv)=>pX(X,z0,z1,Y0,Y1,(c,r,Y,z)=>brick(c,r,X,Y,z,z,hv,side(X),Math.abs(X)*F/(z*z)));
const fwall=(z,X0,X1,Y0,Y1,hv)=>pZ(z,X0,X1,Y0,Y1,(c,r,X,Y)=>brick(c,r,X,Y,z,X,hv,3,F/z));

/* ---------- the sky, and the far end of the lane ---------- */
function sky(){const st=SKY[time][WI?1:0],n=st.length;
for(let r=0;r<H;r++)for(let c=0;c<W;c++){const t=cl(r/90,0,1)*(n-1);let i=Math.floor(t);if(t-i>dith(c,r))i++;own(c,r,rgb(st[Math.min(n-1,i)]),0);}}
function farEnd(){/* a house facing up the lane across the cross street, its door under a fanlight between two lamps */
pY(0,-14,10,ZX,ZE,(c,r,X,z)=>put(c,r,WI?K.snow:K.asphalt,WI?-.1:0,X,0,z,0));
fwall(ZE,-14,10,0,11,9);
for(const wx of[-9,-6.5,-4,1.2,3.4,6])for(const[y0,y1]of[[1.4,3.2],[4.4,6.2],[7.4,9]]){const lt=!day&&hsh(wx*9|0,y0*7|0)<(night?.6:.4);
face(ZE,wx,wx+1,y0,y1,K.glass,0);if(lt)pZ(ZE,wx+.15,wx+.85,y0+.15,y1-.15,(c,r)=>own(c,r,WARM[hsh(c,r*3)*2|0],0));}
for(const[y0,y1]of[[4.4,6.2],[7.4,9]])face(ZE,-1.6,-.6,y0,y1,K.glass,0);
face(ZE,-1.7,-.1,0,3,K.trim,0);face(ZE,-1.4,-.4,0,2.4,K.door,-.1);pZ(ZE,-1.4,-.4,2.45,2.85,(c,r,X,Y)=>day?put(c,r,K.glass,0,X,Y,ZE,3):own(c,r,WARM[1],0));
face(ZE,-14,10,10.6,11,K.trim,-.2);if(WI)face(ZE,-14,10,11,11.15,K.snow,.1);lamp(-2.3,ZE-.6,1);lamp(.5,ZE-.6,1);}

/* ---------- house fronts ---------- */
const FLOORS=[[1.45,3.75],[4.85,6.95],[8,9.7],[10.7,11.8]];
function window_(X,za,zb,y0,y1,fl,id){const sd=side(X),lt=!day&&hsh(id,fl*13+5)<(night?.38:.26)&&fl<3,s=F/((za+zb)/2),wpx=Math.abs(sx(X,za)-sx(X,zb));
const wing=X<0?.48:.42;
for(const[a,b]of[[za-wing,za],[zb,zb+wing]])pX(X,a,b,y0,y1,(c,r,Y,z)=>put(c,r,K.shutter,(.075*s>=2&&Math.floor(Y/.075)%2)?.1:0,X,Y,z,sd));
fill(X,za,zb,y0,y1,K.trim,0);
const ia=za+.09,ib=zb-.09,g0=y0+.1,g1=y1-.1;
pX(X,ia,ib,g0,g1,(c,r,Y,z)=>{const u=(z-ia)/(ib-ia),w=(Y-g0)/(g1-g0),mun=wpx>=6&&(Math.abs(u-1/3)<.045||Math.abs(u-2/3)<.045)||(g1-g0)*s>=9&&(Math.abs(w-.5)<.03||(wpx>=6&&(Math.abs(w-.25)<.025||Math.abs(w-.75)<.025)));
if(lt)mun?put(c,r,K.iron,.2,X,Y,z,sd):own(c,r,mix(WARM[hsh(id,fl)*4|0],WARM[3],w<.2?.4:0),z);
else put(c,r,mun?K.trim:K.glass,mun?-.1:(day?.2+w*.15:0)+(hsh(id,fl)-.5)*.1,X,Y,z,sd);});
fill(X,za-.08,zb+.08,y1,y1+.22,K.granite,.05);fill(X,za-.06,zb+.06,y0-.09,y0,K.granite,.05);
if(WI){fill(X,za-.1,zb+.1,y1+.22,y1+.3,K.snow,.1);fill(X,za-.08,zb+.08,y0,y0+.07,K.snow,.1);for(const[a,b]of[[za-wing,za],[zb,zb+wing]])fill(X,a,b,y1-.07,y1,K.snow,.05);}
if(fl===0)box(X,za,zb,y0,id);}
/* window boxes, planted for the season */
function box(X,za,zb,y0,id){const sd=side(X);fill(X,za-.05,zb+.05,y0-.5,y0-.12,K.wood,-.05);
pX(X,za-.08,zb+.08,y0-.95,y0+.45,(c,r,Y,z)=>{const h=hsh(c*3+id,r*5),up=Y>y0-.12,dn=Y<y0-.5;if(!up&&!dn)return;
const top=up?1-(Y-y0+.12)/.57:(Y-y0+.95)/.45;if(h>top*(up?1.1:.55))return;
let m=WI?K.wreath:AU&&hsh(c,r)<.5?K.pumpkin:K.plant,v=(hsh(c+1,r)-.5)*.2;
if(up&&!WI&&hsh(c*7,r*11+id)<(AU?.25:SP?.3:.2))m=SP?(hsh(c,id)<.5?K.yellow:K.purple):AU?K.yellow:K.red;
if(WI&&hsh(c*5,r*3)<.1)m=K.red;if(WI&&up&&Y>y0+.25)m=K.snow;
put(c,r,m,v,X,Y,z,sd);});}
/* a granite stoop of three steps, the lowest reaching furthest out */
function stoop(za,zb){for(let k=2;k>=0;k--){const h=.233*(k+1),xo=XL+.75-.25*k;
face(za,XL,xo,0,h,K.granite,-.1);fill(xo,za,zb,0,h,K.granite,0);flat(h,XL,xo,za,zb,WI?K.snow:K.granite,WI?.05:.12);
if(WI)face(za,XL,xo,h-.06,h,K.snow,0);}}
/* a door set into the wall (.15 m on the left, .3 on the right): each pixel of the opening is followed in to whatever it meets,
the door, the far reveal, the soffit or the sill. The door is six-panelled: raised stiles and rails, recessed panels. */
function door(X,za,zb,col,lit_,id){const sd=side(X),rx=X+(X<0?-.15:.3),T=3.4,B=.7,tm=X<0?K.trim:K.granite;
fill(X,za-.1,zb+.1,B,T+.1,tm,X<0?0:-.15);fill(X,za-.14,zb+.14,T+.1,T+.22,tm,-.05);if(WI)fill(X,za-.14,zb+.14,T+.22,T+.29,K.snow,.05);
pX(X,za,zb,B,T,(c,r,Y,z)=>{const zr=z*rx/X,Yr=E-(r+.5-VY)*zr/F;
if(zr>zb)return brick(c,r,(c+.5-VX)*zb/F,E-(r+.5-VY)*zb/F,zb,(c+.5-VX)*zb/F,id,3,F/zb,-.25);
if(Yr>T)return put(c,r,K.trim,-.35,rx,T,zr,3);if(Yr<B)return put(c,r,K.granite,0,rx,B,zr,0);
const u=(zr-za)/(zb-za);if(Yr>3.02&&Math.abs(u-1/3)>.035&&Math.abs(u-2/3)>.035)return lit_?own(c,r,WARM[(Yr>3.22)|0],zr):put(c,r,K.glass,0,rx,Yr,zr,sd);
if(Yr>2.94)return put(c,r,K.trim,-.1,rx,Yr,zr,sd);
const fr=u<.12||u>.88||Math.abs(u-.5)<.06||Yr<B+.2||Yr>2.75||Math.abs(Yr-1.65)<.07||Math.abs(Yr-2.25)<.06;put(c,r,K[col],fr?.05:-.06,rx,Yr,zr,sd);});
const kz=za+(zb-za)*.82;put(Math.round(sx(rx,kz)-.5),Math.round(sy(1.45,kz)-.5),K.yellow,.2,rx,1.45,kz,sd);
if(WI&&X<0)wreath(rx+.06,2.05,(za+zb)/2,-Math.PI/2,.24,sd,(c,r)=>{const zf=F*X/(c+.5-VX);return zf>=za&&zf<=zb;});}
/* a slate hood over a door, as on the second house in the photo */
function hood(za,zb){const a=za-.25,b=zb+.25,o=XL+.38;
flat(3.66,XL,o,a,b,K.slate,-.2);face(a,XL,o,3.66,3.96,K.slate,0);fill(o,a,b,3.66,3.96,K.slate,.05);
if(WI){face(a,XL,o,3.9,3.96,K.snow,0);fill(o,a,b,3.9,3.96,K.snow,.05);}}
/* a wreath hung in its own plane: th 0 faces up the lane at the camera, -PI/2 faces out from the left-hand fronts. Each pixel is
followed back to that plane, so it foreshortens with whatever it hangs on. Snow along the top, a red bow at the bottom. */
function wreath(X,Y,z,th,R,sd,clip){const nx=-Math.sin(th),nz=-Math.cos(th),ux=Math.cos(th),uz=-Math.sin(th),cx=sx(X,z),cy=sy(Y,z),hb=R*F/z+2;
for(let r=Math.floor(cy-hb);r<=cy+hb;r++)for(let c=Math.floor(cx-hb);c<=cx+hb;c++){if(clip&&!clip(c,r))continue;
const dx=(c+.5-VX)/F,dn=nx*dx+nz;if(Math.abs(dn)<1e-6)continue;const t=(nx*X+nz*z)/dn,px_=t*dx-X,pz=t-z,a=ux*px_+uz*pz,b=E-(r+.5-VY)*t/F-Y,d=Math.hypot(a,b)/R;
if(Math.abs(a)<=.32*R&&Math.abs(b+.74*R)<=.08*R+.3*Math.abs(a)){put(c,r,K.red,.1,X,Y,z,sd);continue;}
if(d>1||d<.5)continue;const sn=b>.2*R&&hsh(c,r)<.75;put(c,r,sn?K.snow:K.wreath,(hsh(c*3,r)-.5)*.3+(sn?.1:0)-(d>.88||d<.6?.12:0),X,Y,z,sd);}}

/* windows are 1 m wide with their shutters folded back either side; a house fits two beside its door if it is 5.6 m wide, else one */
const bays=(z0,z1,dz,dn)=>(z1-z0>=5.6?[1.75,3.55]:[1.75]).map(o=>dn?dz+o:dz-o).filter(b=>b>z0+.45&&b+1<z1-.45);
function leftHouses(){for(let i=LH.length-1;i>=0;i--){const[z0,z1,ht,dn,col,hd]=LH[i],top=ht;
const prev=i?LH[i-1][2]:0;if(i&&ht>prev)fwall(z0,XL-12,XL,prev,ht,i);
wall(XL,z0,z1,0,top,i);fill(XL,z0,z1,0,.45,K.granite,-.05);fill(XL,z0,z1,top-.32,top,K.trim,-.2);fill(XL,z0,z1,top-.4,top-.32,K.slate,0);
if(WI)fill(XL,z0,z1,top,top+.12,K.snow,.05);
if(i%3===1)fwall(z1-.4,XL-1.2,XL-.4,top,top+1.6,i+40);
const dz=dn?z0+.42:z1-1.42,bz=i?bays(z0,z1,dz,dn):[];
door(XL,dz,dz+1,col,!day&&(i===0||hsh(i,77)<.5),i);if(hd)hood(dz,dz+1);
/* the first door has a lantern on the wall beside it, as the doors in the night photo do */
if(!i)lamp(XL+.18,dz-.3,2,.5);
for(let f=0;f<FLOORS.length;f++){const[a,b]=FLOORS[f];if(b>ht-.6)continue;
bz.forEach((w,k)=>window_(XL,w,w+1,a,b,f,i*10+k));if(f)window_(XL,dz,dz+1,a,b,f,i*10+5);}}
/* the first house: a downpipe where the photo has one */
fill(XL,4.36,4.46,0,14,K.iron,0);}
function leftStoops(){for(let i=LH.length-1;i>=0;i--){const[z0,z1,,dn]=LH[i],dz=dn?z0+.42:z1-1.42;stoop(dz-.15,dz+1.15);}}

function rightSide(){for(let i=RH.length-1;i>0;i--){const[z0,z1,ht,kind]=RH[i];
const prev=RH[i-1][2];if(ht>prev)fwall(z0,XR,XR+12,prev,ht,i+20);
wall(XR,z0,z1,0,ht,i+20);fill(XR,z0,z1,0,.4,K.granite,-.05);
if(kind===1){fill(XR,z0,z1,ht-.14,ht,K.granite,.05);if(WI)fill(XR,z0,z1,ht,ht+.1,K.snow,.05);
/* ivy along the coping: Boston ivy, which turns crimson in the fall */
else pX(XR,z0,z1,ht-1.4,ht+.3,(c,r,Y,z)=>{const t=(Y-ht+1.4)/1.7,n=hsh(c*5,r*7);if(n<(1-Math.abs(t-.55)*1.8)*.85)put(c,r,AU&&hsh(c>>1,r>>1)<.6?K.red:K.plant,(hsh(c,r*3)-.5)*.25,XR,Y,z,2);});continue;}
fill(XR,z0,z1,ht-.3,ht,K.trim,-.2);if(WI)fill(XR,z0,z1,ht,ht+.12,K.snow,.05);
const n=Math.round((z1-z0)/2.1);for(let k=0;k<n;k++){const za=z0+.5+k*(z1-z0-.6)/n,isD=k===0;
if(isD)door(XR,za,za+1,'door',!day&&hsh(i,k)<.5,i*10+k);
for(let f=isD?1:0;f<FLOORS.length;f++){const[a,b]=FLOORS[f];if(b>ht-.6)continue;window_(XR,za+.1,za+1,a,b,f,i*10+k+100);}}}}

/* ---------- the ground ---------- */
function ground(){pY(0,XL,XR,1.5,ZX,(c,r,X,z)=>{const s=F/z;let m,v=0;
if(WI){/* fresh snow, drifted against the walls, with a few lines of footprints through it */
v=(X<XL+.4?.1:0)+(X>XR-.15?.06:0)+(hsh(Math.floor(X*3),Math.floor(z*1.5))-.5)*.05;if(Math.abs(X-CL)<.07||Math.abs(X-CR)<.07)v-=.1;
for(const p of[-.55+.22*Math.sin(z*.6),(XL+CL)/2+.05+.15*Math.sin(z*.9+1),.9])if(Math.abs(X-p)<.3){const k=Math.floor(z/.34),fx=(X-p-(k&1?.11:-.11))/.075,fz=(z-k*.34-.17)/.13;if(fx*fx+fz*fz<1&&hsh(k,p*10|0)<.8)v-=.22;}
return put(c,r,K.snow,v,X,0,z,0);}
if(X>CL&&X<CR){/* cobbles: rounded setts in rows, lit on top; far off they blur into texture */
const gz=z/.13,ri=Math.floor(gz),gx=X/.15+(ri&1?.5:0),ci=Math.floor(gx),fx=gx-ci-.5,fz=gz-ri-.5,d=fx*fx+fz*fz*1.3,hv=hsh(ci,ri);
if(.13*E*F/(z*z)>=1.6&&.15*s>=2.5){m=d>.2?K.gap:K.cobble;v=m===K.gap?0:(hv-.5)*.22-fz*.2;}else{m=K.cobble;v=(hsh(c,r)-.5)*.12-.05;}
if(X<CL+.2||X>CR-.2)v-=.06;}
else if(X<=CL-.16||X>=CR+.16){m=K.walk;const bz=z/.1,bi=Math.floor(bz),bx=X/.2+(bi&1?.5:0),jx=bx-Math.floor(bx),jz=bz-bi;
v=(hsh(Math.floor(bx),bi)-.5)*.14;if(E*F*.1/(z*z)>=2.2&&(jz<.22||(.2*s>=4&&jx<.1)))m=K.gap,v=.15;}
else{m=K.granite;v=.08;if(Math.abs(X-CL)<.025||Math.abs(X-CR)<.025)v-=.25;}
if(AU&&hsh(Math.floor(X*14),Math.floor(z*9))<(z>5&&z<22?.16:.05))m=hsh(Math.floor(X*5),Math.floor(z*3))<.5?K.pumpkin:K.leaf,v=(hsh(c,r)-.5)*.2;
put(c,r,m,v,X,0,z,0);});}

/* ---------- gas lamps ---------- */
/* kind 1 on a post, 0 on a bracket off the left-hand fronts, 2 a small lantern on the wall by a door; k scales it.
Proportions from the near lamp in the dusk photo: the head 31 px from finial to base there, the glass 14 px across,
dim in its upper part, the mantle low down. */
function lamp(X,z,kind,k){k=k||1;const s=F/z,cx=sx(X,z),Y0=kind===2?2.2:2.66,b=Y0+.08*k,g1=Y0+.6*k,mY=Y0+.28*k,top=Y0+.9*k;
const row=(Ya,Yb,w0,w1,f)=>span(sy(Yb,z),sy(Ya,z),r=>{const Y=E-(r+.5-VY)*z/F,t=(Y-Ya)/(Yb-Ya),w=(w0+(w1-w0)*t)*k*s/2;span(cx-w,cx+w,c=>f(c,r,Y,(c+.5-cx)/Math.max(w,.5)));});
/* ironwork faces 5: the light is inside the glass, so neither it nor the halo brightens the frame round it */
const iron=v=>(c,r,Y,u)=>put(c,r,K.iron,v+(u<-.3?.12:0),X,Y,z,5);
if(kind===1){row(0,.12,.3,.26,iron(0));row(.12,.9,.22,.15,iron(0));row(.9,2.58,.12,.1,iron(0));row(2.56,2.66,.17,.17,iron(.05));}
else{const ay=Math.round(sy(top+.05,z)-.5);span(sx(XL,z),cx,c=>put(c,ay,K.iron,.05,X,top+.05,z,5));row(top,top+.05,.04,.04,iron(0));}
row(Y0,b,.12,.26,iron(0));
const gc=(day?['#d8cc9e','#fff0b8','#a8a490']:dusk?['#ffc868','#fff2c4','#c89a6a']:['#ffb850','#fff6dc','#d08a48']).map(rgb);
/* the glass, pixel by pixel: a frame round it, a bar across its upper third, and once it is nine pixels across the corner post
between the front pane and the narrow side pane seen from up the lane; a small mantle low in the front pane, the glow falling off round it */
const r0=Math.ceil(sy(g1,z)-.5),r1=Math.floor(sy(b,z)-.5),rows=r1-r0+1,rb=rows>=8?Math.round(r0+rows*.3):-9;
for(let r=r0;r<=r1;r++){const t=(sy(b,z)-r-.5)/(sy(b,z)-sy(g1,z)),w=(.3+.1*t)*k*s/2,c0=Math.round(cx-w),c1=Math.max(c0,Math.round(cx+w)-1),n=c1-c0+1,
pc=n>=9?c0+Math.max(2,Math.round(n*.22)):c0,fc=n>=9?(pc+1+c1)/2:(c0+c1)/2,mr=r0+(rows-1)*.62,Y=E-(r+.5-VY)*z/F;
for(let c=c0;c<=c1;c++){if(n>=5&&(c===c0||c===c1||c===pc||r===rb||(rows>=6&&(r===r0||r===r1)))){put(c,r,K.iron,.08,X,Y,z,5);continue;}
const d=Math.hypot(c-fc,(r-mr)*.8),core=d<=(n>=9?1.2:.6);let col=mix(gc[0],gc[1],Math.max(0,1-d/(n*.42)));
if(r<rb)col=mix(col,gc[2],.5);if(c<pc)col=mix(col,gc[2],.4);own(c,r,core?gc[1]:col,z*.3);if(core)flames.push([c,r]);}}
row(g1,g1+.04*k,.42,.42,iron(.1));row(g1+.04*k,Y0+.81*k,.44,.08,(c,r,Y,u)=>WI&&Y>g1+.08*k?put(c,r,K.snow,.15,X,Y,z,3):put(c,r,K.iron,u<0?.14:0,X,Y,z,5));
row(Y0+.81*k,top,.05,.05,iron(0));
if(WI&&kind===1)wreath(X-.09,2.1,z-.05,Math.PI/3,.22,3);
/* the halo, in screen space: brightest beside the glass */
if(!day){const hy=sy(mY,z),hr=((night?1.2:.85)*s+3)*k;for(let r=Math.floor(hy-hr);r<=hy+hr;r++)for(let c=Math.floor(cx-hr);c<=cx+hr;c++){if(c<0||c>=W||r<0||r>=H)continue;const d=Math.hypot(c+.5-cx,r+.5-hy)/hr;if(d<1)GW[r*W+c]+=(1-d)*(1-d)*(night?1:.6);}}}

/* ---------- the tree behind the garden wall ---------- */
function canopy(){const sparse=SP?.1:AU?.06:.03;let sd=5;const rn=()=>((sd=(sd*16807)%2147483647)/2147483647);
const edge=r=>{const lr=crownAt(r+.5);return lr&&[lr[0]+(hsh(r>>2,1)-.5)*7+Math.sin(r*.31)*2.5,lr[1]+(hsh(r>>2,2)-.5)*5];};
const leaf=(c,r,v,ck)=>{const z=7+(206-c)*.11;put(c,r,AU?(ck<.25?K.pumpkin:ck<.4?K.yellow:K.leaf):K.leaf,v-r*.0015,(c+.5-VX)*z/F,E-(r+.5-VY)*z/F,z,4);};
/* the shadowed inside of the crown, with a few holes where there is sky behind */
for(let r=0;r<96;r++){const e=edge(r);if(!e)continue;for(let c=Math.ceil(e[0]+3);c<e[1]-3;c++){const i=r*W+c;if(r<40&&hsh(c>>1,r>>1)<sparse&&!M[i]&&!PZ[i])continue;leaf(c,r,-.28+(hsh(c,r)-.5)*.08,hsh(c>>3,r>>3));}}
/* then clumps, each a dome lit from above, the lower ones over the upper */
const cs=[];for(let k=0;k<170;k++){const r=rn()*99,e=edge(r|0);if(e)cs.push([e[0]+rn()*(e[1]-e[0]),r,2.5+rn()*3.5,rn()]);}
cs.sort((a,b)=>a[1]-b[1]);
for(const[cx,cy,R0,ck]of cs)for(let r=Math.floor(cy-R0);r<=cy+R0;r++)for(let c=Math.floor(cx-R0);c<=cx+R0;c++){const dx=(c+.5-cx)/R0,dy=(r+.5-cy)/R0,d=dx*dx+dy*dy;
if(d>1-hsh(c*7,r*3)*.35)continue;leaf(c,r,-(dx*.35+dy)*.12-d*.08+(ck-.5)*.1+(hsh(c*3,r*7)<.08?.12:0),ck);}}
function bareTree(){/* winter: a trunk rising from behind the wall, limbs over the lane, snow along their tops */
let sd=11;const rn=()=>((sd=(sd*16807)%2147483647)/2147483647);
const dot=(c,r,m,v)=>{const lr=crownAt(r+.5);if(r>=0&&(!lr||c<lr[0]-5||c>lr[1]+5))return;put(c,r,m,v,1.6,7,10,4);};
const line=(x0,y0,x1,y1,w)=>{const n=Math.ceil(Math.hypot(x1-x0,y1-y0)),fl=Math.abs(x1-x0)>Math.abs(y1-y0)*.6;
for(let k=0;k<=n;k++){const t=k/n,px_=x0+(x1-x0)*t,py_=y0+(y1-y0)*t,ww=Math.max(1,Math.round(w*(1-t*.5)));
for(let o=0;o<ww;o++)dot(Math.round(px_-ww/2+o),Math.round(py_),K.bark,o?0:.12);if(fl&&rn()<.85)dot(Math.round(px_),Math.round(py_)-Math.ceil(ww/2),K.snow,.15);}};
/* twigs: fine enough that the snow on them is most of what shows, as in the photo */
const twig=(x0,y0,a,len,d,v)=>{const x1=x0+Math.cos(a)*len,y1=y0+Math.sin(a)*len,n=Math.ceil(len);
for(let k=1;k<=n;k++)dot(Math.round(x0+(x1-x0)*k/n),Math.round(y0+(y1-y0)*k/n),K.snow,v);
if(d>0)for(const s_ of[-1,1])twig(x1,y1,a+s_*(.3+rn()*.4),len*(.62+rn()*.2),d-1,v-.06);};
const tx=sx(TREE[0],TREE[1]),ty=sy(4.2,TREE[1]),fork=[tx-5,ty-40];line(tx,ty,fork[0],fork[1],4);
for(const[x0,y0,x1,y1,w]of[[tx-1,ty-10,153,86,2],[tx-2,ty-22,149,34,3],[fork[0],fork[1],150,2,3],[fork[0],fork[1],176,-4,2],[fork[0],fork[1],204,-2,2],[tx-2,ty-30,160,58,2]]){
/* each limb bends three times on its way out, thinning as it goes */
const L=Math.hypot(x1-x0,y1-y0),nx=-(y1-y0)/L,ny=(x1-x0)/L,pts=[[x0,y0]];for(let k=1;k<4;k++){const o=(rn()-.5)*L*.14;pts.push([x0+(x1-x0)*k/4+nx*o,y0+(y1-y0)*k/4+ny*o]);}pts.push([x1,y1]);
let a=0;for(let k=0;k<4;k++){const[a0,b0]=pts[k],[a1,b1]=pts[k+1];line(a0,b0,a1,b1,w*(1-k*.2));a=Math.atan2(b1-b0,a1-a0);
for(let t=0;t<1;t+=.34)twig(a0+(a1-a0)*t,b0+(b1-b0)*t,a+(rn()<.5?-1:1)*(.5+rn()*.6),4+rn()*6*(1.3-k*.25),3,-.08);}
twig(x1,y1,a-.4,6,2,-.12);twig(x1,y1,a+.4,6,2,-.12);}}

/* ---------- the near corner: a boxwood in a planter, and a pot by the second stoop ---------- */
function planters(){const shrub=(X,z,Rw,Rh,kind)=>{const s=F/z,cx=sx(X,z),by=sy(.3,z),cy=by-Rh*s*.9;
face(z,X-Rw*.8,X+Rw*.8,0,.3,K.granite,-.1);
for(let r=Math.floor(cy-Rh*s);r<by;r++)for(let c=Math.floor(cx-Rw*s);c<=cx+Rw*s;c++){const d=Math.hypot((c+.5-cx)/(Rw*s),(r+.5-cy)/(Rh*s));
if(d+(hsh(c,r*7)-.5)*.25>1)continue;const tp=(cy-r)/(Rh*s);let m=kind,v=tp*.2-d*.12+(hsh(c,r)-.5)*.16;
if(WI&&tp>.25&&hsh(c,r*3)<.85)m=K.snow,v=.05;else if(kind===K.plant&&!WI&&hsh(c*5,r*9)<.22)m=SP?K.yellow:AU?K.pumpkin:K.pink,v+=.08;
put(c,r,m,v,X,.6,z,4);}};
shrub(-2.85,3.4,.4,.3,K.wreath);shrub(-3.42,6.3,.26,.28,K.plant);
if(AU)for(const[X,z,Y,rs]of[[-3.55,13.45,.467,.2],[-3.3,13.85,.233,.17],[-3.35,6.6,0,.15]]){const s=F/z,cx=sx(X,z),cy=sy(Y+rs*.8,z);
for(let r=Math.floor(cy-rs*s);r<=cy+rs*s*.8;r++)for(let c=Math.floor(cx-rs*s*1.2);c<=cx+rs*s*1.2;c++){const u=(c+.5-cx)/(rs*s*1.2),w=(r+.5-cy)/(rs*s*.8);if(u*u+w*w>1)continue;
put(c,r,K.pumpkin,-w*.25-Math.abs(u)*.15+((Math.abs(u*4+.5)%1)<.2?-.12:0),X,Y,z,3);}put(Math.round(cx),Math.round(cy-rs*s*.8)-1,K.plant,0,X,Y,z,3);}}

/* the flag, from a pole off the left houses, where the photo has one */
function flag(){const z=40,s=F/z,x0=sx(XL,z),y0=sy(5.6,z),x1=sx(XL+1.2,z-.3),y1=sy(6.3,z-.3),n=Math.ceil(Math.hypot(x1-x0,y1-y0));
for(let k=0;k<=n;k++){const c=Math.round(x0+(x1-x0)*k/n),r=Math.round(y0+(y1-y0)*k/n);put(c,r,K.trim,-.1,XL,5.8,z,1);
if(k>0)for(let j=1;j<=Math.round(.9*s);j++){const cant=k>n*.55&&j<=Math.round(.45*s);put(c,r+j,cant?K.blue:j%2?K.red:K.trim,cant?0:-.05,XL+.5,5.2,z,3);}}}

/* ---------- light ---------- */
/* snow and white paint throw back more of whatever light there is */
function shade(){const amb=AMB[time]*(WI?(night?1.3:.95):1),alb=[],lk=LK[time],hz=HAZE[time],hc=rgb(WI?(night?'#56606e':day?'#c8d0d8':'#6a6c84'):hz[0]),ha=WI?hz[1]+.15:hz[1];
const Ls=LAMPS.map(([X,z])=>[X,2.94,z,lk*1.3,10]);Ls.push([XL+.2,2.34,4.73,lk*.75,3]);
alb[K.snow]=day?.22:.16;alb[K.trim]=.06;
const tan=day&&!WI?{spring:.95,summer:1.4,autumn:.7}[season]:dusk&&!WI?.12:0,sk=day?.22:.14;
const RR=[null].concat(MAT.map(n=>R11(n==='leaf'?LEAF[season]:RAMP[n]))),glow=rgb(night?'#ffc870':'#f6b878');
for(let i=0;i<N;i++){const c=i%W,r=(i/W)|0;let col;
if(!M[i]){col=C[i];if(PZ[i])col=mix(col,hc,ha*cl((PZ[i]-12)/50,0,1)*.5);if(GW[i]>0){const g=Math.min(1,GW[i]);if(g*.9>dith(c,r)*.9+.1)col=mix(col,glow,g*.35);}}
else{const X=PX[i],Y=PY[i],z=PZ[i],n=NR[i];let v=amb*(.78+.22*Math.min(1,Y/9))+V[i]+(alb[M[i]]||0);
/* the sun, low over the right-hand roofs, reaches only the upper left-hand fronts; over the garden it is the tree that casts the shade */
if(tan&&n!==2&&Y+(XR-X)*tan>(z>5.2&&z<14?11:topOf(RH,z))+.2)v+=sk;
if(n!==5)for(const[lx,ly,lz,k,r2]of Ls){const dx=lx-X,dy=ly-Y,dz=lz-z,d2=dx*dx+dy*dy+dz*dz,d=Math.sqrt(d2);
const lam=n===0?dy/d:n===1?dx/d:n===2?-dx/d:n===3?-dz/d:.5,w=(lam+.4)/1.4;if(w>0)v+=k*w*Math.exp(-d2/r2);}
if(n!==5)v+=GW[i]*.3;let t=v*10,j=Math.floor(t);if(t-j>dith(c,r))j++;if(day&&M[i]===K.snow&&j>8)j=8;col=RR[M[i]][cl(j,0,10)];
col=mix(col,hc,ha*cl((z-10)/45,0,1));}
base[i*4]=col[0];base[i*4+1]=col[1];base[i*4+2]=col[2];base[i*4+3]=255;}}

function build(){M.fill(0);GW.fill(0);flames=[];
sky();farEnd();rightSide();leftHouses();ground();leftStoops();
for(const[X,z,p]of LAMPS)if(z>15)lamp(X,z,p);
flag();if(!WI)canopy();else bareTree();planters();
/* the near house on the right, in front of the tree: a downpipe, a door, a window box */
wall(XR,2,5.2,0,14,20);fill(XR,2,5.2,0,.4,K.granite,-.05);fill(XR,4.5,4.62,0,14,K.trim,-.2);
door(XR,3.75,4.45,'door',false,7);window_(XR,3.6,4.5,3.3,5.5,0,7);
for(const[X,z,p]of LAMPS)if(z<=15)lamp(X,z,p);
shade();}

/* ---------- animation: the mantles flicker; snow falls, leaves fall ---------- */
const flakes=Array.from({length:110},(_,i)=>({x:hsh(i,1)*W,y:hsh(i,2)*H,v:.3+hsh(i,3)*.6,d:hsh(i,4)*6.28}));
const bits=Array.from({length:10},(_,i)=>({x:140+hsh(i,5)*70,y:hsh(i,6)*120,v:.2+hsh(i,7)*.2,d:hsh(i,8)*6.28}));
let tick=0,still=!!opts.still;
function frame(){if(!still)tick++;
img.data.set(base);x.putImageData(img,0,0);
for(let k=0;k<flames.length;k++){const[c,r]=flames[k];if(!still&&hsh(k,tick>>1)<.25){x.fillStyle=day?'#f2e2a8':'#ffe6a6';x.fillRect(c,r,1,1);}}
if(WI){const fc=day?['#ffffff','#e6ebf2']:dusk?['#f2eef4','#c8c4d6']:['#e8eaf0','#a8adbd'];
for(const f of flakes){if(!still){f.y+=f.v;f.d+=.05;f.x+=Math.sin(f.d)*.3-.15;if(f.y>H){f.y=-2;f.x=Math.random()*W;}if(f.x<0)f.x+=W;}
x.fillStyle=f.v>.65?fc[0]:fc[1];x.fillRect(Math.round(f.x),Math.round(f.y),f.v>.8?2:1,f.v>.8?2:1);}}
if(AU){const lc=['#e8a03a','#c4622b','#e8c25a'];for(const q of bits){if(!still){q.d+=.08;q.x+=-.25+.35*Math.sin(q.d);q.y+=q.v+.1*Math.sin(q.d*1.3);if(q.y>H){q.y=hsh(tick,3)*40;q.x=140+Math.random()*70;}}
x.fillStyle=lc[Math.floor((Math.sin(q.d*1.7)+1)*1.4999)];x.fillRect(Math.round(q.x),Math.round(q.y),Math.sin(q.d*2.3)>.2?2:1,1);}}}

function label(){return'Pixel-art view down a Beacon Hill lane: brick rowhouses with black shutters on both sides, gas lamps, a cobbled lane between brick sidewalks, a tree leaning over a garden wall, in '+season+' '+{day:'daylight',dusk:'at dusk',night:'at night'}[time]+'.';}
function applyLook(l){l=l||{};if(SEASONS.indexOf(l.season)>=0)season=l.season;if(TIMES.indexOf(l.time)>=0)time=l.time;
WI=season==='winter';AU=season==='autumn';SP=season==='spring';day=time==='day';dusk=time==='dusk';night=time==='night';
build();canvas.setAttribute('aria-label',label());}

/* whole-number zoom in device pixels, so every art pixel is the same size */
function fit(){const dpr=G.devicePixelRatio||1,el=opts.container||canvas.parentElement||document.body,cs=G.getComputedStyle(el);
const avail=el.clientWidth-(parseFloat(cs.paddingLeft)||0)-(parseFloat(cs.paddingRight)||0);
let s=Math.max(1,Math.floor(avail*dpr/W+1e-6)),mh=typeof opts.maxHeight==='function'?opts.maxHeight():opts.maxHeight;if(opts.maxScale)s=Math.min(s,opts.maxScale);if(mh)s=Math.min(s,Math.max(1,Math.floor(mh*dpr/H+1e-6)));
canvas.style.width=W*s/dpr+'px';canvas.style.height=H*s/dpr+'px';}

let raf=0,last=0,dead=false;
const loop=t=>{raf=requestAnimationFrame(loop);if(t-last<66)return;last=t;frame();};
const halt=()=>{if(raf){cancelAnimationFrame(raf);raf=0;}};
const go=()=>{if(!raf&&!still&&!dead&&!document.hidden)raf=requestAnimationFrame(loop);};
const mq=G.matchMedia&&G.matchMedia('(prefers-reduced-motion: reduce)');
const setStill=()=>{still=!!opts.still||!!(mq&&mq.matches);if(still){halt();tick=0;frame();}else go();};
const onVis=()=>{if(document.hidden)halt();else go();};
const onMq=()=>setStill();
let ro=null;
G.addEventListener('resize',fit);document.addEventListener('visibilitychange',onVis);
if(mq&&mq.addEventListener)mq.addEventListener('change',onMq);
if(G.ResizeObserver&&(opts.container||canvas.parentElement)){ro=new G.ResizeObserver(fit);ro.observe(opts.container||canvas.parentElement);}
/* paint now: the loop draws on animation frames, which a hidden tab or a headless browser may not deliver for a while */
applyLook({season:opts.season||'summer',time:opts.time||'dusk'});fit();frame();setStill();
return{setLook(l){applyLook(l);if(still){tick=0;frame();}else if(!raf)go();},look:()=>({season,time}),
stop(){dead=true;halt();G.removeEventListener('resize',fit);document.removeEventListener('visibilitychange',onVis);if(mq&&mq.removeEventListener)mq.removeEventListener('change',onMq);if(ro)ro.disconnect();}};}

G.CooldownScene={mount,lookFor,SEASONS,TIMES,size:[W,H]};
})(typeof window!=='undefined'?window:this);
