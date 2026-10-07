#property copyright "MT5 Bot"
#property version   "2.10"
#property strict
#property indicator_chart_window
#property indicator_buffers 0
#property indicator_plots   0
#property description "Displays the current bot proposal. Manual trading only."

// Headerless ASCII row, exactly 18 semicolon-delimited fields:
// 0 version, 1 state (active/experimental/waiting), 2 symbol, 3 timeframe, 4 direction,
// 5 entry, 6 zone_low, 7 zone_high, 8 stop, 9 target, 10 digits,
// 11 observed_utc, 12 valid_until_utc, 13 bar_utc, 14 broker_offset_minutes,
// 15 terminal_key, 16 binding_nonce_hex, 17 binding_sha256_hex.
// The helper publishes an atomic snapshot; the indicator never extends it.
#define OVERLAY_FIELD_COUNT 18
#define OVERLAY_TTL_SECONDS 25
#define OVERLAY_MAX_BYTES   2048

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

void ClearLevels()
  {
   string suffixes[]={"zone","entry","stop","target","entry_text","stop_text","target_text","summary","details"};
   for(int i=0;i<ArraySize(suffixes);i++) ObjectDelete(0,g_prefix+suffixes[i]);
  }

void ShowWaiting(const string message)
  {
   ClearLevels();
   if(!SetLabel("status","MT5 Bot | "+message,22,clrSilver))
      Print("MT5 Bot: display update failed");
   ChartRedraw(0);
  }

bool DrawProposal(const Proposal &p)
  {
   datetime start=(datetime)((long)p.bar+(long)p.offset_minutes*60);
   datetime end=(datetime)((long)p.valid_until+(long)p.offset_minutes*60);
   string zone=g_prefix+"zone";
   color direction_color=(p.direction=="BUY" ? clrLimeGreen : clrOrange);
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
   long seconds=(long)p.valid_until-(long)TimeGMT();
   return(SetLine("entry",p.entry,clrGold) && SetLine("stop",p.stop,clrTomato)
          && SetLine("target",p.target,clrLimeGreen)
          && SetPriceText("entry_text","Entry "+entry,label_time,p.entry,clrGold)
          && SetPriceText("stop_text","SL "+stop,label_time,p.stop,clrTomato)
          && SetPriceText("target_text","TP "+target,label_time,p.target,clrLimeGreen)
          && SetLabel("status","MT5 Bot | "+(p.experimental ? "Demo experimental | " : "")+p.direction+" proposal | "+IntegerToString(seconds)+"s",22,direction_color,12)
          && SetLabel("summary","Entry zone: "+DoubleToString(p.zone_low,p.digits)+" - "+DoubleToString(p.zone_high,p.digits),43,clrGold)
          && SetLabel("details",(p.experimental ? "Estimated costs | No certified win rate | " : "")+"Reference levels | Review before manual trade",63,clrSilver));
  }

void UpdateOverlay()
  {
   if(MQLInfoInteger(MQL_TESTER)) { ShowWaiting("Live terminal required"); return; }
   if(_Symbol!="XAUUSD" || _Period!=PERIOD_M1) { ShowWaiting("Use XAUUSD / M1: M15 direction + M5 confirmation"); return; }
   if(g_terminal_key=="") { ShowWaiting("Terminal folder unavailable"); return; }
   if(AccountInfoInteger(ACCOUNT_TRADE_MODE)!=ACCOUNT_TRADE_MODE_DEMO) { ShowWaiting("Demo account required"); return; }
   if(AccountInfoInteger(ACCOUNT_LOGIN)!=g_session_login || AccountInfoString(ACCOUNT_SERVER)!=g_session_server)
      g_account_changed=true;
   if(g_account_changed) { ShowWaiting("Account changed: reload indicator"); return; }
   if(!TerminalInfoInteger(TERMINAL_CONNECTED)) { ShowWaiting("MT5 disconnected"); return; }
   Proposal p;
   string message="";
   if(!ReadProposal(p,message)) { ShowWaiting(message); return; }
   // Recheck after file/hash/history reads; no expired levels are drawn.
   if(TimeGMT()>=p.valid_until || AccountInfoInteger(ACCOUNT_LOGIN)!=g_session_login
      || AccountInfoString(ACCOUNT_SERVER)!=g_session_server
      || AccountInfoInteger(ACCOUNT_TRADE_MODE)!=ACCOUNT_TRADE_MODE_DEMO
      || !TerminalInfoInteger(TERMINAL_CONNECTED))
     { ShowWaiting("Proposal expired or account changed"); return; }
   if(!DrawProposal(p)) { ShowWaiting("Display unavailable"); return; }
   if(TimeGMT()>=p.valid_until) { ShowWaiting("Proposal expired"); return; }
   ChartRedraw(0);
  }

int OnInit()
  {
   g_terminal_key=TerminalKey();
   g_source_file="MT5Bot\\levels_"+g_terminal_key+".csv";
   g_session_login=AccountInfoInteger(ACCOUNT_LOGIN);
   g_session_server=AccountInfoString(ACCOUNT_SERVER);
   g_account_changed=false;
   string base="_MBL_"+IntegerToString(ChartID())+"_"+IntegerToString((long)GetTickCount64())+"_";
   int instance=0;
   do { g_prefix=base+IntegerToString(instance)+"_"; instance++; }
   while(ObjectFind(0,g_prefix+"status")>=0);
   IndicatorSetString(INDICATOR_SHORTNAME,"MT5 Bot Levels");
   if(!EventSetTimer(1)) return(INIT_FAILED);
   UpdateOverlay();
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   if(StringLen(g_prefix)>0) ObjectsDeleteAll(0,g_prefix,0,-1);
   ChartRedraw(0);
  }

void OnTimer() { UpdateOverlay(); }

void OnChartEvent(const int id,const long &lparam,const double &dparam,const string &sparam)
  {
   if(id==CHARTEVENT_CHART_CHANGE) UpdateOverlay();
  }

int OnCalculate(const int rates_total,const int prev_calculated,const datetime &time[],
                const double &open[],const double &high[],const double &low[],const double &close[],
                const long &tick_volume[],const long &volume[],const int &spread[])
  {
   return(rates_total);
  }
