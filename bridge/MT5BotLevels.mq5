#property copyright "MT5 Bot"
#property version   "2.13"
#property strict
#property indicator_chart_window
#property indicator_buffers 0
#property indicator_plots   0
#property description "Four-frame readiness panel and validated Demo bot proposal levels."

#include <Canvas\Canvas.mqh>

// Headerless ASCII row, exactly 18 semicolon-delimited fields:
// 0 version, 1 state (active/experimental/waiting), 2 symbol, 3 timeframe, 4 direction,
// 5 entry, 6 zone_low, 7 zone_high, 8 stop, 9 target, 10 digits,
// 11 observed_utc, 12 valid_until_utc, 13 bar_utc, 14 broker_offset_minutes,
// 15 terminal_key, 16 binding_nonce_hex, 17 binding_sha256_hex.
// The helper publishes an atomic snapshot; the indicator never extends it.
#define OVERLAY_FIELD_COUNT 18
#define OVERLAY_TTL_SECONDS 25
#define OVERLAY_MAX_BYTES   2048
#define READINESS_FIELD_COUNT 10
#define READINESS_TTL_SECONDS 25
#define READINESS_MAX_BYTES   2048

struct Proposal
  {
   bool     active;
   bool     experimental;
   string   direction;
   double   entry;
   double   zone_low;
   double   zone_high;
   double   stop;
   double   target;
   int      digits;
   datetime observed;
   datetime valid_until;
   datetime bar;
   int      offset_minutes;
  };

string g_prefix="";
string g_terminal_key="";
string g_source_file="";
string g_readiness_file="";
string g_notice_key="";
long   g_session_login=0;
string g_session_server="";
bool   g_account_changed=false;

bool IsSafeMarker(const string text)
  {
   int length=StringLen(text);
   if(length<1 || length>80) return(false);
   for(int i=0;i<length;i++)
     {
      ushort c=StringGetCharacter(text,i);
      if(!((c>='a' && c<='z') || (c>='0' && c<='9') || c=='_' || c=='-'))
         return(false);
     }
   return(true);
  }

string TerminalKey()
  {
   string path=TerminalInfoString(TERMINAL_DATA_PATH);
   StringReplace(path,"/","\\");
   while(StringLen(path)>0 && StringSubstr(path,StringLen(path)-1)=="\\")
      path=StringSubstr(path,0,StringLen(path)-1);
   int last=-1;
   for(int i=0;i<StringLen(path);i++)
      if(StringGetCharacter(path,i)=='\\') last=i;
   string key=StringSubstr(path,last+1);
   StringToLower(key);
   return(IsSafeMarker(key) ? key : "");
  }

bool IsLowerHex(const string text,const int length)
  {
   if(StringLen(text)!=length) return(false);
   for(int i=0;i<length;i++)
     {
      ushort c=StringGetCharacter(text,i);
      if(!((c>='0' && c<='9') || (c>='a' && c<='f'))) return(false);
     }
   return(true);
  }

bool AccountBindingMatches(const string nonce,const string expected)
  {
   if(!IsLowerHex(nonce,32) || !IsLowerHex(expected,64)) return(false);
   long login=AccountInfoInteger(ACCOUNT_LOGIN);
   string server=AccountInfoString(ACCOUNT_SERVER);
   if(login<=0 || StringLen(server)==0) return(false);
   // Raw account identity stays in process memory and is never displayed.
   string material=nonce+"\n"+g_terminal_key+"\n"+server+"\n"+IntegerToString(login);
   uchar data[],key[],digest[];
   int copied=StringToCharArray(material,data,0,WHOLE_ARRAY,CP_UTF8);
   if(copied<2 || data[copied-1]!=0) return(false);
   ArrayResize(data,copied-1); // Exclude the UTF8 NUL from the Python-compatible hash.
   if(CryptEncode(CRYPT_HASH_SHA256,data,key,digest)!=32) return(false);
   string actual="";
   for(int i=0;i<32;i++) actual+=StringFormat("%02x",(int)digest[i]);
   return(actual==expected);
  }

string OpportunityNoticeKey()
  {
   // Bridge nonces and chart/timeframe IDs change on reload; use a stable,
   // anonymous account+terminal identity only for display notification dedup.
   // Raw identity remains in memory and never enters a notice or variable name.
   string material="MT5BotOpportunity/v1\n"+g_terminal_key+"\n"+g_session_server+"\n"
                   +IntegerToString(g_session_login)+"\nXAUUSD\nM1";
   uchar data[],key[],digest[];
   int copied=StringToCharArray(material,data,0,WHOLE_ARRAY,CP_UTF8);
   if(copied<2 || data[copied-1]!=0) return("");
   ArrayResize(data,copied-1);
   if(CryptEncode(CRYPT_HASH_SHA256,data,key,digest)!=32) return("");
   string anonymous="";
   for(int i=0;i<24;i++) anonymous+=StringFormat("%02x",(int)digest[i]);
   return("MT5BotNotice_"+anonymous); // 61 ASCII characters, one key per binding.
  }

bool ClaimOpportunityNotice(const Proposal &p)
  {
   if(g_notice_key=="" || p.bar<=0) return(false);
   // Temporary terminal state survives chart reloads and is removed by MT5
   // on terminal exit. Never overwrite/reset another instance's last notice.
   if(!GlobalVariableCheck(g_notice_key) && !GlobalVariableTemp(g_notice_key))
      return(false);
   double previous=0.0;
   if(!GlobalVariableGet(g_notice_key,previous) || !MathIsValidNumber(previous)
      || previous<0.0 || previous!=MathFloor(previous)
      || previous>(double)((long)TimeGMT()*4+3)) return(false);
   long last_bar=(long)previous/4;
   int mask=(int)((long)previous%4);
   int direction_bit=(p.direction=="BUY" ? 1 : 2);
   if((long)p.bar<last_bar || ((long)p.bar==last_bar && (mask & direction_bit)!=0))
      return(false);
   int next_mask=((long)p.bar==last_bar ? (mask | direction_bit) : direction_bit);
   double token=(double)((long)p.bar*4+next_mask);
   // Only one chart instance can claim this new bar/direction atomically.
   return(GlobalVariableSetOnCondition(g_notice_key,token,previous));
  }

void NotifyOpportunity(const Proposal &p)
  {
   if(!p.active || TimeGMT()>=p.valid_until
      || AccountInfoInteger(ACCOUNT_LOGIN)!=g_session_login
      || AccountInfoString(ACCOUNT_SERVER)!=g_session_server
      || AccountInfoInteger(ACCOUNT_TRADE_MODE)!=ACCOUNT_TRADE_MODE_DEMO
      || !TerminalInfoInteger(TERMINAL_CONNECTED)) return;
   string notice="MT5 Bot | "+(p.experimental ? "Demo experimental | " : "Demo | ")
                 +p.direction+" XAUUSD opportunity\n"
                 +"Entry "+DoubleToString(p.entry,p.digits)+" | Zone "
                 +DoubleToString(p.zone_low,p.digits)+" - "+DoubleToString(p.zone_high,p.digits)+"\n"
                 +"SL "+DoubleToString(p.stop,p.digits)+" | TP "+DoubleToString(p.target,p.digits)+"\n"
                 +"Expires UTC "+TimeToString(p.valid_until,TIME_DATE|TIME_SECONDS)+"\n"
                 +(p.experimental ? "Estimated costs | No certified win rate\n" : "")
                 +"Review first. Telegram Accept requests one guarded Demo attempt; wait for MT5 confirmation.";
   if(!ClaimOpportunityNotice(p) || TimeGMT()>=p.valid_until
      || AccountInfoInteger(ACCOUNT_LOGIN)!=g_session_login
      || AccountInfoString(ACCOUNT_SERVER)!=g_session_server
      || AccountInfoInteger(ACCOUNT_TRADE_MODE)!=ACCOUNT_TRADE_MODE_DEMO
      || !TerminalInfoInteger(TERMINAL_CONNECTED)) return;
   Alert(notice);
  }

bool ParseInteger(const string text,long &value,const bool signed_value=false)
  {
   int length=StringLen(text),start=0;
   if(length<1 || length>12) return(false);
   if(signed_value && StringGetCharacter(text,0)=='-') start=1;
   if(start==length) return(false);
   for(int i=start;i<length;i++)
     {
      ushort c=StringGetCharacter(text,i);
      if(c<'0' || c>'9') return(false);
     }
   value=StringToInteger(text);
   return(true);
  }

bool ParsePrice(const string text,double &value)
  {
   int length=StringLen(text),dots=0,digits=0;
   if(length<1 || length>32) return(false);
   for(int i=0;i<length;i++)
     {
      ushort c=StringGetCharacter(text,i);
      if(c=='.') dots++;
      else if(c>='0' && c<='9') digits++;
      else return(false);
     }
   if(dots>1 || digits==0 || StringGetCharacter(text,0)=='.'
      || StringGetCharacter(text,length-1)=='.') return(false);
   value=StringToDouble(text);
   return(MathIsValidNumber(value) && value>=0.0 && value<10000000.0);
  }

bool OnTickGrid(const double price,const double tick,const int digits)
  {
   if(!MathIsValidNumber(tick) || tick<=0.0 || price<=0.0) return(false);
   double tolerance=MathMax(tick*0.000001,0.00000001);
   return(MathAbs(price-MathRound(price/tick)*tick)<=tolerance
          && MathAbs(price-NormalizeDouble(price,digits))<=tolerance);
  }

bool ReadProposal(Proposal &p,string &message)
  {
   message="Waiting for a valid proposal";
   int handle=FileOpen(g_source_file,FILE_READ|FILE_BIN|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE);
   if(handle==INVALID_HANDLE) return(false);
   ulong size=FileSize(handle);
   if(size<1 || size>OVERLAY_MAX_BYTES)
     {
      FileClose(handle);
      message="Waiting: invalid proposal file";
      return(false);
     }
   uchar bytes[];
   uint copied=FileReadArray(handle,bytes,0,(int)size);
   bool unchanged=(FileSize(handle)==size && FileTell(handle)==size);
   FileClose(handle);
   if(copied!=size || !unchanged)
     {
      message="Waiting: incomplete proposal file";
      return(false);
     }
   for(int i=0;i<(int)size;i++)
     {
      // Quotes, whitespace, NUL, CR/LF, BOM and multiple rows are not accepted.
      if(bytes[i]<33 || bytes[i]>126 || bytes[i]=='"')
        {
         message="Waiting: invalid proposal format";
         return(false);
        }
     }
   string fields[];
   string row=CharArrayToString(bytes,0,(int)size,CP_UTF8);
   if(StringSplit(row,';',fields)!=OVERLAY_FIELD_COUNT
      || fields[0]!="2" || (fields[1]!="active" && fields[1]!="experimental" && fields[1]!="waiting")
      || fields[2]!="XAUUSD" || fields[3]!="M1")
     {
      message="Waiting: invalid proposal format";
      return(false);
     }
   if(fields[15]!=g_terminal_key || !AccountBindingMatches(fields[16],fields[17]))
     {
      message="Waiting: terminal or account changed";
      return(false);
     }
   long digits=0,observed=0,valid_until=0,bar=0,offset=0;
   if(!ParsePrice(fields[5],p.entry) || !ParsePrice(fields[6],p.zone_low)
      || !ParsePrice(fields[7],p.zone_high) || !ParsePrice(fields[8],p.stop)
      || !ParsePrice(fields[9],p.target) || !ParseInteger(fields[10],digits)
      || !ParseInteger(fields[11],observed) || !ParseInteger(fields[12],valid_until)
      || !ParseInteger(fields[13],bar) || !ParseInteger(fields[14],offset,true)
      || digits<0 || digits>8 || offset<-720 || offset>840 || offset%15!=0)
     {
      message="Waiting: invalid proposal values";
      return(false);
     }
   datetime now=TimeGMT();
   if(observed<946684800 || observed>(long)now || valid_until<=observed
      || valid_until-observed>OVERLAY_TTL_SECONDS || (long)now-observed>OVERLAY_TTL_SECONDS
      || (long)now>=valid_until)
     {
      message="Waiting: proposal expired or feed offline";
      return(false);
     }
   p.experimental=(fields[1]=="experimental");
   p.active=(fields[1]=="active" || p.experimental);
   p.direction=fields[4];
   p.digits=(int)digits;
   p.observed=(datetime)observed;
   p.valid_until=(datetime)valid_until;
   p.bar=(datetime)bar;
   p.offset_minutes=(int)offset;
   if(!p.active)
     {
      message="No current proposal: waiting for market and risk conditions";
      if(p.direction!="NONE" || p.entry!=0.0 || p.zone_low!=0.0 || p.zone_high!=0.0
         || p.stop!=0.0 || p.target!=0.0 || bar!=0)
         message="Waiting: invalid empty proposal";
      return(false);
     }
   double tick=SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_SIZE);
   if(digits!=(long)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS)
      || !OnTickGrid(p.entry,tick,p.digits) || !OnTickGrid(p.zone_low,tick,p.digits)
      || !OnTickGrid(p.zone_high,tick,p.digits) || !OnTickGrid(p.stop,tick,p.digits)
      || !OnTickGrid(p.target,tick,p.digits) || p.zone_low>p.entry || p.entry>p.zone_high
      || p.zone_low>=p.zone_high)
     {
      message="Waiting: invalid price grid";
      return(false);
     }
   bool ordered=(p.direction=="BUY" && p.stop<p.zone_low && p.zone_high<p.target)
                || (p.direction=="SELL" && p.target<p.zone_low && p.zone_high<p.stop);
   datetime chart_bar=(datetime)(bar+offset*60);
   if(!ordered || bar<=0 || bar%60!=0 || bar+60>observed || observed-bar>135
      || (p.experimental && (observed>bar+90 || valid_until>bar+90))
      || iBarShift(_Symbol,PERIOD_M1,chart_bar,true)<1)
     {
      message="Waiting: invalid direction or chart time";
      return(false);
     }
   return(true);
  }

bool PrepareObject(const string suffix,const ENUM_OBJECT type,const datetime t=0,const double price=0.0)
  {
   string name=g_prefix+suffix;
   if(ObjectFind(0,name)<0 && !ObjectCreate(0,name,type,0,t,price)) return(false);
   return(ObjectSetInteger(0,name,OBJPROP_SELECTABLE,false)
          && ObjectSetInteger(0,name,OBJPROP_SELECTED,false)
          && ObjectSetInteger(0,name,OBJPROP_HIDDEN,true));
  }

bool SetLabel(const string suffix,const string text,const int y,const color shade,const int font_size=11)
  {
   string name=g_prefix+suffix;
   return(PrepareObject(suffix,OBJ_LABEL)
          && ObjectSetInteger(0,name,OBJPROP_CORNER,CORNER_LEFT_UPPER)
          && ObjectSetInteger(0,name,OBJPROP_ANCHOR,ANCHOR_LEFT_UPPER)
          && ObjectSetInteger(0,name,OBJPROP_XDISTANCE,14)
          && ObjectSetInteger(0,name,OBJPROP_YDISTANCE,y)
          && ObjectSetInteger(0,name,OBJPROP_FONTSIZE,font_size)
          && ObjectSetInteger(0,name,OBJPROP_COLOR,shade)
          && ObjectSetString(0,name,OBJPROP_FONT,"Arial")
          && ObjectSetString(0,name,OBJPROP_TEXT,text));
  }

bool SetLine(const string suffix,const double price,const color shade)
  {
   string name=g_prefix+suffix;
   if(!PrepareObject(suffix,OBJ_HLINE,0,price)
      || !ObjectSetDouble(0,name,OBJPROP_PRICE,0,price)
      || !ObjectSetInteger(0,name,OBJPROP_COLOR,shade)
      || !ObjectSetInteger(0,name,OBJPROP_WIDTH,2)) return(false);
   double actual=0.0;
   return(ObjectGetDouble(0,name,OBJPROP_PRICE,0,actual)
          && MathAbs(actual-price)<0.0000001);
  }

bool SetPriceText(const string suffix,const string text,const datetime t,const double price,const color shade)
  {
   string name=g_prefix+suffix;
   if(!PrepareObject(suffix,OBJ_TEXT,t,price) || !ObjectMove(0,name,0,t,price)
      || !ObjectSetInteger(0,name,OBJPROP_ANCHOR,ANCHOR_RIGHT_LOWER)
      || !ObjectSetInteger(0,name,OBJPROP_FONTSIZE,11)
      || !ObjectSetInteger(0,name,OBJPROP_COLOR,shade)
      || !ObjectSetString(0,name,OBJPROP_FONT,"Arial")
      || !ObjectSetString(0,name,OBJPROP_TEXT,text)) return(false);
   double actual_price=0.0;
   long actual_time=0;
   return(ObjectGetDouble(0,name,OBJPROP_PRICE,0,actual_price)
          && ObjectGetInteger(0,name,OBJPROP_TIME,0,actual_time)
          && MathAbs(actual_price-price)<0.0000001 && actual_time==(long)t);
  }

// Read-only telemetry. These counts never authorize a proposal or a notice.
struct Readiness
  {
   datetime observed;
   datetime valid_until;
   int counts[4]; // H1, M15, M5, M1 closed candles, each bounded to 0..64.
  };

bool ReadReadiness(Readiness &r)
  {
   if(MQLInfoInteger(MQL_TESTER) || _Symbol!="XAUUSD" || g_terminal_key==""
      || g_account_changed || AccountInfoInteger(ACCOUNT_LOGIN)!=g_session_login
      || AccountInfoString(ACCOUNT_SERVER)!=g_session_server
      || AccountInfoInteger(ACCOUNT_TRADE_MODE)!=ACCOUNT_TRADE_MODE_DEMO
      || !TerminalInfoInteger(TERMINAL_CONNECTED)) return(false);
   int handle=FileOpen(g_readiness_file,FILE_READ|FILE_BIN|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE);
   if(handle==INVALID_HANDLE) return(false);
   ulong size=FileSize(handle);
   if(size<1 || size>READINESS_MAX_BYTES) { FileClose(handle); return(false); }
   uchar bytes[];
   uint copied=FileReadArray(handle,bytes,0,(int)size);
   bool unchanged=(FileSize(handle)==size && FileTell(handle)==size);
   FileClose(handle);
   if(copied!=size || !unchanged) return(false);
   for(int i=0;i<(int)size;i++)
      if(bytes[i]<33 || bytes[i]>126 || bytes[i]=='"') return(false);
   string fields[];
   string row=CharArrayToString(bytes,0,(int)size,CP_UTF8);
   if(StringSplit(row,';',fields)!=READINESS_FIELD_COUNT || fields[0]!="1"
      || fields[7]!=g_terminal_key || !AccountBindingMatches(fields[8],fields[9])) return(false);
   long observed=0,valid_until=0;
   if(!ParseInteger(fields[1],observed) || !ParseInteger(fields[2],valid_until)) return(false);
   for(int i=0;i<4;i++)
     {
      long count=0;
      if(!ParseInteger(fields[i+3],count) || count<0 || count>64) return(false);
      r.counts[i]=(int)count;
     }
   datetime now=TimeGMT();
   if(observed<946684800 || observed>(long)now || valid_until<=observed
      || valid_until-observed>READINESS_TTL_SECONDS || (long)now-observed>READINESS_TTL_SECONDS
      || (long)now>=valid_until || g_account_changed
      || AccountInfoInteger(ACCOUNT_LOGIN)!=g_session_login
      || AccountInfoString(ACCOUNT_SERVER)!=g_session_server
      || AccountInfoInteger(ACCOUNT_TRADE_MODE)!=ACCOUNT_TRADE_MODE_DEMO
      || !TerminalInfoInteger(TERMINAL_CONNECTED)) return(false);
   r.observed=(datetime)observed;
   r.valid_until=(datetime)valid_until;
   return(true);
  }


// Presentation state belongs only to this chart. No control submits a trade.
CCanvas g_panel;
bool g_panel_collapsed=false;
int g_panel_x=12,g_panel_y=16,g_panel_width=0,g_panel_height=0;
datetime g_snapshot_observed=0,g_snapshot_deadline=0;
bool g_waiting_snapshot=false;
Proposal g_panel_proposal;
ulong g_last_panel_click=0;
struct PanelHit
  {
   int x,y,width,height;
   string action;
  };
PanelHit g_panel_hits[];

string ViewPeriodName(const ENUM_TIMEFRAMES period)
  {
   if(period==PERIOD_M1) return("M1");
   if(period==PERIOD_M5) return("M5");
   if(period==PERIOD_M15) return("M15");
   if(period==PERIOD_H1) return("H1");
   string name=EnumToString(period);
   StringReplace(name,"PERIOD_","");
   return(name);
  }

void RoundedPanelRect(const int x,const int y,const int width,const int height,const int radius,const uint shade)
  {
   if(width<1 || height<1) return;
   int r=(int)MathMin(radius,MathMin(width/2,height/2));
   if(r<1) { g_panel.FillRectangle(x,y,x+width-1,y+height-1,shade); return; }
   g_panel.FillRectangle(x+r,y,x+width-r-1,y+height-1,shade);
   g_panel.FillRectangle(x,y+r,x+width-1,y+height-r-1,shade);
   g_panel.FillCircle(x+r,y+r,r,shade);
   g_panel.FillCircle(x+width-r-1,y+r,r,shade);
   g_panel.FillCircle(x+r,y+height-r-1,r,shade);
   g_panel.FillCircle(x+width-r-1,y+height-r-1,r,shade);
  }

void PanelText(const int x,const int y,const string text,const int size,const color shade,const bool bold=false)
  {
   g_panel.FontSet("Segoe UI",-size*10,bold ? FW_BOLD : 0);
   g_panel.TextOut(x,y,text,ColorToARGB(shade,255));
  }

void PanelTextFit(const int x,const int y,const string text,const int width,const int size,const color shade,const bool bold=false)
  {
   if(width<1) return;
   g_panel.FontSet("Segoe UI",-size*10,bold ? FW_BOLD : 0);
   string fitted=text;
   if(g_panel.TextWidth(fitted)>width)
     {
      while(StringLen(fitted)>0 && g_panel.TextWidth(fitted+"...")>width)
         fitted=StringSubstr(fitted,0,StringLen(fitted)-1);
      fitted+="...";
     }
   g_panel.TextOut(x,y,fitted,ColorToARGB(shade,255));
  }

void PanelWrappedText(const int x,const int y,const string text,const int width,const int max_lines,const int size,const color shade)
  {
   g_panel.FontSet("Segoe UI",-size*10,0);
   string words[];
   int count=StringSplit(text,' ',words),row=0;
   string line="";
   for(int i=0;i<count;i++)
     {
      string candidate=(line=="" ? words[i] : line+" "+words[i]);
      if(line!="" && g_panel.TextWidth(candidate)>width)
        {
         PanelTextFit(x,y+row*18,line,width,size,shade);
         row++;
         if(row>=max_lines-1)
           {
            string remaining=words[i];
            for(int j=i+1;j<count;j++) remaining+=" "+words[j];
            PanelTextFit(x,y+row*18,remaining,width,size,shade);
            return;
           }
         line=words[i];
        }
      else line=candidate;
     }
   if(line!="") PanelTextFit(x,y+row*18,line,width,size,shade);
  }

void PanelButton(const string action,const string label,const int x,const int y,const int width,const int height,const bool selected=false,const bool primary=false)
  {
   color background=selected ? C'48,111,219' : (primary ? C'34,88,179' : C'26,42,68');
   RoundedPanelRect(x,y,width,height,7,ColorToARGB(background,255));
   g_panel.FontSet("Segoe UI",-90,selected ? FW_BOLD : 0);
   int text_width=g_panel.TextWidth(label),text_height=g_panel.TextHeight(label);
   g_panel.TextOut(x+(width-text_width)/2,y+(height-text_height)/2,label,
                   ColorToARGB(selected || primary ? clrWhite : C'175,191,213',255));
   int index=ArraySize(g_panel_hits);
   ArrayResize(g_panel_hits,index+1);
   g_panel_hits[index].x=x;
   g_panel_hits[index].y=y;
   g_panel_hits[index].width=width;
   g_panel_hits[index].height=height;
   g_panel_hits[index].action=action;
  }

bool EnsurePanelCanvas(const int width,const int height)
  {
   string name=g_prefix+"panel";
   if(g_panel_width!=width || g_panel_height!=height || ObjectFind(0,name)<0)
     {
      g_panel.Destroy();
      g_panel_width=0;
      g_panel_height=0;
      if(!g_panel.CreateBitmapLabel(0,0,name,g_panel_x,g_panel_y,width,height,COLOR_FORMAT_ARGB_NORMALIZE))
         return(false);
      g_panel_width=width;
      g_panel_height=height;
     }
   return(ObjectSetInteger(0,name,OBJPROP_CORNER,CORNER_LEFT_UPPER)
          && ObjectSetInteger(0,name,OBJPROP_ANCHOR,ANCHOR_LEFT_UPPER)
          && ObjectSetInteger(0,name,OBJPROP_SELECTABLE,false)
          && ObjectSetInteger(0,name,OBJPROP_SELECTED,false)
          && ObjectSetInteger(0,name,OBJPROP_HIDDEN,true)
          && ObjectSetInteger(0,name,OBJPROP_BACK,false)
          && ObjectSetInteger(0,name,OBJPROP_ZORDER,1000)
          && ObjectSetString(0,name,OBJPROP_TOOLTIP,"\n"));
  }

bool DrawPanel(const bool active,const string message)
  {
   int chart_width=(int)ChartGetInteger(0,CHART_WIDTH_IN_PIXELS);
   int chart_height=(int)ChartGetInteger(0,CHART_HEIGHT_IN_PIXELS);
   int width=(int)MathMin(420,chart_width-24);
   if(width<80 || chart_height<80) return(false);
   int body_base=active ? 380 : 326;
   int expanded_height=body_base+128;
   bool compact=g_panel_collapsed || width<300 || chart_height<expanded_height+32;
   int height=compact ? (int)MathMin(106,chart_height-32) : expanded_height;
   if(!EnsurePanelCanvas(width,height)) return(false);
   ArrayResize(g_panel_hits,0);
   g_panel.Erase(0);
   RoundedPanelRect(0,0,width,height,16,ColorToARGB(C'10,20,37',248));
   RoundedPanelRect(1,1,width-2,height-2,15,ColorToARGB(C'13,24,43',255));

   bool connected=(bool)TerminalInfoInteger(TERMINAL_CONNECTED);
   datetime now=TimeGMT();
   bool main_fresh=(active && now<g_panel_proposal.valid_until)
                   || (g_waiting_snapshot && now<g_snapshot_deadline);
   Readiness readiness;
   ZeroMemory(readiness);
   bool telemetry=ReadReadiness(readiness) && main_fresh;
   string state=active ? g_panel_proposal.direction : (main_fresh ? "WAITING" : "CHECK FEED");
   color state_color=active ? (state=="BUY" ? C'77,216,164' : C'255,143,121') : C'238,190,97';
   string live=telemetry ? "LIVE" : (!connected ? "OFFLINE" : (main_fresh ? "NO UPDATE" : "CHECK FEED"));
   color live_color=telemetry ? C'77,216,164' : C'238,190,97';
   string clock=TimeToString(now,TIME_SECONDS)+" UTC";
   PanelTextFit(18,13,_Symbol,width-180,20,clrWhite,true);
   if(width>=300) PanelText(168,20,"2.13",8,C'143,162,188');
   string account_label=AccountInfoInteger(ACCOUNT_TRADE_MODE)==ACCOUNT_TRADE_MODE_DEMO ? "Demo" : "Demo required";
   PanelTextFit(18,47,"Chart "+ViewPeriodName(_Period)+"  |  "+account_label,width-160,9,C'143,162,188');

   if(compact)
     {
      if(width>=300) PanelText(width-124,21,clock,8,C'143,162,188');
      if(height>=96)
        {
         RoundedPanelRect(18,69,86,23,6,ColorToARGB(C'34,45,62',255));
         PanelText(27,72,state,8,state_color,true);
         PanelButton("open","Open",width-90,65,72,29,false,true);
        }
      else if(width>=180)
         PanelButton("open","Open",width-70,15,58,27,false,true);
      g_panel.Update(false);
      return(true);
     }

   RoundedPanelRect(width-124,17,106,25,7,ColorToARGB(telemetry ? C'22,67,54' : C'54,47,35',255));
   g_panel.FontSet("Segoe UI",-80,FW_BOLD);
   int badge_width=g_panel.TextWidth(live);
   g_panel.TextOut(width-124+(106-badge_width)/2,21,live,ColorToARGB(live_color,255));
   PanelText(width-124,48,clock,8,C'143,162,188');
   PanelTextFit(18,73,"H1 / M15 / M5 / M1",width-36,16,clrWhite,true);
   g_panel.FillRectangle(18,104,width-19,104,ColorToARGB(C'36,51,73',255));
   PanelText(18,114,"CLOSED CANDLES",8,C'143,162,188',true);
   PanelTextFit(width/2,114,"22 needed per frame",width/2-18,8,C'143,162,188');
   string readiness_frames[]={"H1","M15","M5","M1"};
   int tile_width=(width-60)/4;
   int warmup_frame=-1;
   for(int i=0;i<4;i++)
     {
      int x=18+i*(tile_width+8);
      bool ready=telemetry && readiness.counts[i]>=22;
      if(telemetry && readiness.counts[i]<22 && warmup_frame<0) warmup_frame=i;
      RoundedPanelRect(x,136,tile_width,56,8,ColorToARGB(ready ? C'20,49,48' : C'23,37,58',255));
      PanelText(x+10,143,readiness_frames[i],8,C'175,191,213',true);
      string count=telemetry ? IntegerToString((int)MathMin(22,readiness.counts[i]))+"/22" : "--/22";
      PanelTextFit(x+10,162,count,tile_width-20,12,ready ? C'77,216,164' : C'238,190,97',true);
     }
   RoundedPanelRect(18,205,width-36,28,7,ColorToARGB(C'24,38,60',255));
   PanelText(28,210,active ? "M1 SIGNAL: "+state : (main_fresh ? "SIGNAL: WAITING" : "SIGNAL: UNAVAILABLE"),10,state_color,true);
   string snapshot=main_fresh ? "Updated "+IntegerToString((long)now-(long)g_snapshot_observed)+"s ago" : "Update unavailable";
   PanelTextFit(width-148,212,snapshot,120,7,C'143,162,188');

   if(active)
     {
      long seconds=(long)g_panel_proposal.valid_until-(long)now;
      if(seconds<0) seconds=0;
      int card_width=(width-52)/3;
      string captions[]={"ENTRY","STOP LOSS","TAKE PROFIT"};
      double prices[3];
      prices[0]=g_panel_proposal.entry;
      prices[1]=g_panel_proposal.stop;
      prices[2]=g_panel_proposal.target;
      color shades[]={clrGold,C'255,143,121',C'77,216,164'};
      for(int i=0;i<3;i++)
        {
         int x=18+i*(card_width+8);
         RoundedPanelRect(x,246,card_width,53,8,ColorToARGB(C'22,36,58',255));
         PanelText(x+10,254,captions[i],7,C'143,162,188');
         PanelTextFit(x+10,271,DoubleToString(prices[i],g_panel_proposal.digits),card_width-20,12,shades[i],true);
        }
      PanelTextFit(18,310,"Entry zone: "+DoubleToString(g_panel_proposal.zone_low,g_panel_proposal.digits)+" - "+
                   DoubleToString(g_panel_proposal.zone_high,g_panel_proposal.digits),width-36,9,C'175,191,213');
      PanelTextFit(18,330,"Expires in "+IntegerToString(seconds)+"s  |  Review on Telegram",width-36,8,C'175,191,213');
      PanelTextFit(18,348,g_panel_proposal.experimental ? "Demo experimental | Estimated costs" : "Telegram Accept requests a guarded Demo attempt",
                   width-36,8,g_panel_proposal.experimental ? C'238,190,97' : C'143,162,188');
      if(g_panel_proposal.experimental)
         PanelTextFit(18,363,"Performance unproven | No certified win rate",width-36,8,C'238,190,97');
     }
   else
     {
      string reason=message;
      string detail="No trade is prepared or submitted by this panel.";
      if(g_waiting_snapshot && main_fresh && telemetry && warmup_frame>=0)
        {
         reason=readiness_frames[warmup_frame]+" warmup: "+IntegerToString(readiness.counts[warmup_frame])+"/22 closed candles";
         detail="Live feed received. Waiting for enough closed candles.";
        }
      else if(g_waiting_snapshot && main_fresh && telemetry)
        {
         reason="All four frames ready. Waiting for market and risk conditions.";
         detail="Ready candles do not guarantee a signal.";
        }
      PanelWrappedText(18,247,reason,width-36,3,10,C'220,229,242');
      PanelTextFit(18,309,detail,width-36,8,C'143,162,188');
     }

   g_panel.FillRectangle(18,body_base,width-19,body_base,ColorToARGB(C'36,51,73',255));
   PanelText(18,body_base+11,"Chart timeframe",9,C'175,191,213',true);
   PanelTextFit(145,body_base+12,"View only",width-163,8,C'143,162,188');
   string frames[]={"M1","M5","M15","H1"};
   int button_width=(width-54)/4;
   for(int i=0;i<4;i++)
      PanelButton("view_"+frames[i],frames[i],18+i*(button_width+6),body_base+34,button_width,29,
                  ViewPeriodName(_Period)==frames[i]);
   int footer_width=(width-44)/2;
   PanelButton("refresh","Refresh",18,body_base+78,footer_width,30,false,true);
   PanelButton("hide","Hide",26+footer_width,body_base+78,footer_width,30);
   PanelTextFit(18,body_base+113,"Refresh rereads the latest local update.",width-36,7,C'113,135,164');
   g_panel.Update(false);
   return(true);
  }

void HandlePanelClick(const int chart_x,const int chart_y)
  {
   int x=chart_x-g_panel_x,y=chart_y-g_panel_y;
   for(int i=0;i<ArraySize(g_panel_hits);i++)
     {
      PanelHit hit=g_panel_hits[i];
      if(x<hit.x || x>=hit.x+hit.width || y<hit.y || y>=hit.y+hit.height) continue;
      ulong now=GetTickCount64();
      if(g_last_panel_click>0 && now-g_last_panel_click<200) return;
      g_last_panel_click=now;
      if(hit.action=="hide") { g_panel_collapsed=true; UpdateOverlay(); return; }
      if(hit.action=="open") { g_panel_collapsed=false; UpdateOverlay(); return; }
      if(hit.action=="refresh") { UpdateOverlay(); return; }
      ENUM_TIMEFRAMES period=PERIOD_CURRENT;
      if(hit.action=="view_M1") period=PERIOD_M1;
      if(hit.action=="view_M5") period=PERIOD_M5;
      if(hit.action=="view_M15") period=PERIOD_M15;
      if(hit.action=="view_H1") period=PERIOD_H1;
      // Only this chart's view changes. Signal timing stays on completed M1.
      if(period!=PERIOD_CURRENT && period!=_Period)
        {
         if(!ChartSetSymbolPeriod(0,_Symbol,period))
            Print("MT5 Bot: chart view change unavailable");
        }
      else UpdateOverlay();
      return;
     }
  }

void ClearLevels()
  {
   string suffixes[]={"zone","entry","stop","target","entry_text","stop_text","target_text","summary","details"};
   for(int i=0;i<ArraySize(suffixes);i++) ObjectDelete(0,g_prefix+suffixes[i]);
  }

void ShowWaiting(const string message)
  {
   ClearLevels();
   if(!DrawPanel(false,message))
      Print("MT5 Bot: display update failed");
   ChartRedraw(0);
  }

bool DrawProposal(const Proposal &p)
  {
   g_panel_proposal=p;
   datetime start=(datetime)((long)p.bar+(long)p.offset_minutes*60);
   datetime end=(datetime)((long)p.valid_until+(long)p.offset_minutes*60);
   string zone=g_prefix+"zone";
   if(ObjectFind(0,zone)<0 && !ObjectCreate(0,zone,OBJ_RECTANGLE,0,start,p.zone_low,end,p.zone_high))
      return(false);
   if(!ObjectMove(0,zone,0,start,p.zone_low) || !ObjectMove(0,zone,1,end,p.zone_high)
      || !ObjectSetInteger(0,zone,OBJPROP_COLOR,clrDarkSlateGray)
      || !ObjectSetInteger(0,zone,OBJPROP_FILL,true)
      || !ObjectSetInteger(0,zone,OBJPROP_BACK,true)
      || !ObjectSetInteger(0,zone,OBJPROP_SELECTABLE,false)
      || !ObjectSetInteger(0,zone,OBJPROP_HIDDEN,true)) return(false);
   double low=0.0,high=0.0;
   long first=0,last=0;
   if(!ObjectGetDouble(0,zone,OBJPROP_PRICE,0,low) || !ObjectGetDouble(0,zone,OBJPROP_PRICE,1,high)
      || !ObjectGetInteger(0,zone,OBJPROP_TIME,0,first) || !ObjectGetInteger(0,zone,OBJPROP_TIME,1,last)
      || MathAbs(low-p.zone_low)>0.0000001 || MathAbs(high-p.zone_high)>0.0000001
      || first!=(long)start || last!=(long)end) return(false);
   datetime label_time=end;
   int window=0;
   double ignored_price=0.0;
   int width=(int)ChartGetInteger(0,CHART_WIDTH_IN_PIXELS);
   if(width>100)
     {
      datetime visible_right=0;
      if(ChartXYToTimePrice(0,width-20,70,window,visible_right,ignored_price) && window==0)
         label_time=visible_right;
     }
   string entry=DoubleToString(p.entry,p.digits),stop=DoubleToString(p.stop,p.digits),target=DoubleToString(p.target,p.digits);
   return(SetLine("entry",p.entry,clrGold) && SetLine("stop",p.stop,clrTomato)
          && SetLine("target",p.target,clrLimeGreen)
          && SetPriceText("entry_text","Entry "+entry,label_time,p.entry,clrGold)
          && SetPriceText("stop_text","SL "+stop,label_time,p.stop,clrTomato)
          && SetPriceText("target_text","TP "+target,label_time,p.target,clrLimeGreen)
          && DrawPanel(true,""));
  }

void UpdateOverlay()
  {
   g_waiting_snapshot=false;
   if(MQLInfoInteger(MQL_TESTER)) { ShowWaiting("Live terminal required"); return; }
   // The view timeframe does not change the closed-M1 signal/history contract.
   if(_Symbol!="XAUUSD") { ShowWaiting("Use XAUUSD: H1 / M15 / M5 / M1"); return; }
   if(g_terminal_key=="") { ShowWaiting("Terminal folder unavailable"); return; }
   if(AccountInfoInteger(ACCOUNT_TRADE_MODE)!=ACCOUNT_TRADE_MODE_DEMO) { ShowWaiting("Demo account required"); return; }
   if(AccountInfoInteger(ACCOUNT_LOGIN)!=g_session_login || AccountInfoString(ACCOUNT_SERVER)!=g_session_server)
      g_account_changed=true;
   if(g_account_changed) { ShowWaiting("Account changed: reload indicator"); return; }
   if(!TerminalInfoInteger(TERMINAL_CONNECTED)) { ShowWaiting("MT5 disconnected"); return; }
   Proposal p;
   ZeroMemory(p);
   string message="";
   bool accepted=ReadProposal(p,message);
   g_waiting_snapshot=(!accepted && message=="No current proposal: waiting for market and risk conditions"
                       && p.observed>0 && TimeGMT()<p.valid_until);
   if(p.observed>0)
     {
      g_snapshot_observed=p.observed;
      g_snapshot_deadline=p.valid_until;
     }
   if(!accepted) { ShowWaiting(message); return; }
   // Recheck after file/hash/history reads; no expired levels are drawn.
   if(TimeGMT()>=p.valid_until || AccountInfoInteger(ACCOUNT_LOGIN)!=g_session_login
      || AccountInfoString(ACCOUNT_SERVER)!=g_session_server
      || AccountInfoInteger(ACCOUNT_TRADE_MODE)!=ACCOUNT_TRADE_MODE_DEMO
      || !TerminalInfoInteger(TERMINAL_CONNECTED))
     { ShowWaiting("Proposal expired or account changed"); return; }
   if(!DrawProposal(p)) { ShowWaiting("Display unavailable"); return; }
   if(TimeGMT()>=p.valid_until) { ShowWaiting("Proposal expired"); return; }
   ChartRedraw(0);
   NotifyOpportunity(p);
  }

int OnInit()
  {
   g_terminal_key=TerminalKey();
   g_source_file="MT5Bot\\levels_"+g_terminal_key+".csv";
   g_readiness_file="MT5Bot\\readiness_"+g_terminal_key+".csv";
   g_session_login=AccountInfoInteger(ACCOUNT_LOGIN);
   g_session_server=AccountInfoString(ACCOUNT_SERVER);
   g_notice_key=OpportunityNoticeKey();
   g_account_changed=false;
   string base="_MBL_"+IntegerToString(ChartID())+"_"+IntegerToString((long)GetTickCount64())+"_";
   int instance=0;
   do { g_prefix=base+IntegerToString(instance)+"_"; instance++; }
   while(ObjectFind(0,g_prefix+"status")>=0 || ObjectFind(0,g_prefix+"panel")>=0);
   IndicatorSetString(INDICATOR_SHORTNAME,"MT5 Bot Panel 2.13");
   if(!EventSetTimer(1)) return(INIT_FAILED);
   UpdateOverlay();
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   g_panel.Destroy();
   if(StringLen(g_prefix)>0) ObjectsDeleteAll(0,g_prefix,0,-1);
   ChartRedraw(0);
  }

void OnTimer() { UpdateOverlay(); }

void OnChartEvent(const int id,const long &lparam,const double &dparam,const string &sparam)
  {
   if(id==CHARTEVENT_CHART_CHANGE) UpdateOverlay();
   if(id==CHARTEVENT_CLICK || (id==CHARTEVENT_OBJECT_CLICK && sparam==g_prefix+"panel"))
      HandlePanelClick((int)lparam,(int)dparam);
  }

int OnCalculate(const int rates_total,const int prev_calculated,const datetime &time[],
                const double &open[],const double &high[],const double &low[],const double &close[],
                const long &tick_volume[],const long &volume[],const int &spread[])
  {
   return(rates_total);
  }
