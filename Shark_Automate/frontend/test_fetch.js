fetch("http://localhost:3000/api/market/sentiment-history?limit=50")
  .then((r) => r.ok ? r.json() : [])
  .then((rows) => {
    console.log("Is array:", Array.isArray(rows));
    console.log("Length:", rows.length);
  })
  .catch(console.error);
