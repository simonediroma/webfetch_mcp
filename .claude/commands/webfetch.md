Ricorda le regole per usare il tool webfetch MCP in questo progetto:

1. Usa SEMPRE `mcp__webfetch__fetch` — mai il built-in `WebFetch`
2. NON passare `output_format` a meno che l'utente non lo chieda esplicitamente
3. NON usare `extract_text=True` — è deprecato e produce output con CSS noise
4. Il server è configurato con `output_format: trafilatura` come default — omettere il parametro significa usare trafilatura automaticamente
5. Puoi passare `extra_headers` solo se servono header aggiuntivi specifici per la richiesta

Esempio corretto:
```
mcp__webfetch__fetch(url="https://example.com")
```

Esempio sbagliato:
```
mcp__webfetch__fetch(url="https://example.com", output_format="raw")
mcp__webfetch__fetch(url="https://example.com", extract_text=True)
```
