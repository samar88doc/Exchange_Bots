const myOutput = `{
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
    "message": "NEW 1 on {{ticker}} at {{close}} — Price inside STRONG/MAJOR support + Score>=threshold + signal confluence!"
  }
}`;

for (let i = 0; i < 20; i++) {
  console.log(`Pos ${i}: ${myOutput.charCodeAt(i)} ('${myOutput.charAt(i)}')`);
}
