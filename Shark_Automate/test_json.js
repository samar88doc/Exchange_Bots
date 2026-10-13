try {
  JSON.parse(`{
  "event_id": "{{timenow}}-{{ticker}}",
  "strategy_id": "strategy_1",
  "symbol": "{{ticker}}",
  "action": "BUY",
  "timestamp": "{{timenow}}",
  "timeframe": "{{interval}}",
  "price": {{close}},
  "source": "TRADINGVIEW",
  "secret": "demo-webhook-secret-12345",
  "metadata": {
    "message": "NEW 1 on {{ticker}} at {{close}} \u2014 Price inside STRONG/MAJOR support + Score>=threshold + signal confluence!"
  }
}`);
} catch (e) {
  console.log("Full error:", e.message);
}
