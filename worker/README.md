# weatherboard-ambient

The dashboard fetches every feed itself except one. Ambient (DMATStation) needs
an account key, and a public page cannot hold a key, so it goes through here.

`GET /` returns the same shape as `poller.py fetch_ambient()`.

## Deploy

```
npx wrangler login
npx wrangler secret put AMBIENT_APP_KEY     # from ambientweather.net -> Account -> API Keys
npx wrangler secret put AMBIENT_API_KEY
npx wrangler deploy
```

Then set the deployed URL as `AMBIENT_PROXY_URL` in `docs/assets/app.js`, and the
weather card goes live alongside everything else. Leave it empty and the card
falls back to the last committed poll — the board still works.

## Notes

- CORS is limited to `ALLOWED_ORIGINS` in `ambient.js`; add a custom domain there.
- Responses are cached 20s so repeated Refresh clicks cannot trip Ambient's rate
  limit. The station only reports about once a minute anyway.
- This worker holds no GitHub credentials and can write nothing. The only data it
  can reach is the weather at the house, which the dashboard already publishes.
