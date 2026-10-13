const tests = [
  `{\n  "event_id": "{{timenow}}-{{ticker}}"\n}`,
  `{\n  "event_id": "{{timenow}}-{{ticker}}",\n}`,
  `{\n  "event_id": ""{{timenow}}-{{ticker}}",\n}`
];
for(let t of tests) {
  try { JSON.parse(t); } catch(e) { console.log(e.message); }
}
