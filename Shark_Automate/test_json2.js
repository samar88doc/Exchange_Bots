const testCases = [
  `{\n  "event_id": “{{timenow}}-{{ticker}}”,\n  "strategy_id": "strategy_1"\n}`,
  `{\n  “event_id”: "{{timenow}}-{{ticker}}",\n  "strategy_id": "strategy_1"\n}`,
  `{\n  "event_id": "{{timenow}}-{{ticker}}"\n  "strategy_id": "strategy_1"\n}`,
  `{\n  "event_id": "{{timenow}}-{{ticker}}",\n  "strategy_id": "strategy_1"\n}`,
  `{\n  "event_id": {timenow}}-{{ticker}}",\n  "strategy_id": "strategy_1"\n}`
];

testCases.forEach((tc, i) => {
  try {
    JSON.parse(tc);
    console.log(`Test ${i}: OK`);
  } catch(e) {
    console.log(`Test ${i}: ${e.message}`);
  }
});
