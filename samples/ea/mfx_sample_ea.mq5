//+------------------------------------------------------------------+
//| mfx_sample_ea.mq5 - the smallest Expert Advisor that honours     |
//| MFX Hub's contract. Quoted by guides/running-your-own-ea.md.      |
//|                                                                  |
//| What it does, and nothing else:                                  |
//|  * reads mfx\params.json for the parameter version and whether   |
//|    it may trade ("enabled"), every 5 seconds;                    |
//|  * writes a heartbeat line carrying that version every 5 seconds |
//|    to mfx\telemetry.<segment>.ndjson, one file a minute;         |
//|  * writes one "signal" record a minute, which reaches you on the |
//|    terminal's ea channel;                                         |
//|  * trades only when InpTrade is true, MetaTrader allows it and    |
//|    params.json does not say enabled:false. It ships with InpTrade |
//|    false: it places no order unless you change that.             |
//| It opens no file outside MQL5\Files\mfx, loads no library and    |
//| makes no network call.                                           |
//+------------------------------------------------------------------+
#property strict
#include <Trade\Trade.mqh>

input bool InpTrade = false;
input long InpMagic = 4242;

CTrade   g_trade;
datetime g_started = 0;
datetime g_last_signal = 0;
long     g_version = 0;
bool     g_enabled = true;
long     g_signals = 0;

// One telemetry file a minute. A file is sent once it is closed, so a new one each minute keeps
// your records arriving within about a minute of being written.
string Telemetry()
  {
   long segment = 1 + (long)(TimeGMT() - g_started) / 60;
   return StringFormat("mfx\\telemetry.%08I64d.ndjson", segment);
  }

void ReadParams()
  {
   int h = FileOpen("mfx\\params.json", FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(h == INVALID_HANDLE)
      return;                       // no parameter set yet: keep what we have
   string text = "";
   while(!FileIsEnding(h))
      text += FileReadString(h);
   FileClose(h);
   int at = StringFind(text, "\"version\":");
   if(at >= 0)
      g_version = StringToInteger(StringSubstr(text, at + 10, 12));
   g_enabled = StringFind(text, "\"enabled\":false") < 0 && StringFind(text, "\"enabled\": false") < 0;
  }

void Write(const string line)
  {
   int h = FileOpen(Telemetry(), FILE_READ | FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(h == INVALID_HANDLE)
      return;
   FileSeek(h, 0, SEEK_END);
   FileWriteString(h, line);
   FileClose(h);
  }

void Heartbeat()
  {
   Write(StringFormat("{\"ts\":\"%I64d.000\",\"kind\":\"heartbeat\",\"schema_version\":\"1.0\",\"applied_version\":%I64d}\n",
                      (long)TimeGMT(), g_version));
  }

void Signal()
  {
   g_signals++;
   MqlTick tick;
   double bid = SymbolInfoTick(_Symbol, tick) ? tick.bid : 0.0;
   string price = DoubleToString(bid, (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS));
   Write(StringFormat("{\"ts\":\"%I64d.000\",\"kind\":\"signal\",\"n\":%I64d,\"symbol\":\"%s\",\"bid\":%s}\n",
                      (long)TimeGMT(), g_signals, _Symbol, price));
  }

bool MayTrade()
  {
   return InpTrade && g_enabled && MQLInfoInteger(MQL_TRADE_ALLOWED);
  }

int OnInit()
  {
   g_started = TimeGMT();
   g_trade.SetExpertMagicNumber((ulong)InpMagic);
   ReadParams();
   Heartbeat();
   EventSetTimer(5);
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
  }

void OnTimer()
  {
   ReadParams();
   Heartbeat();
   if(TimeGMT() - g_last_signal >= 60)
     {
      g_last_signal = TimeGMT();
      Signal();
      if(MayTrade())
        {
         // Your strategy goes here. Size it in the account's terms, and keep its orders bounded.
        }
     }
  }

void OnTick() {}
//+------------------------------------------------------------------+
