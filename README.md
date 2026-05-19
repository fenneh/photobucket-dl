# photobucket-dl

Pulls your own photos and videos out of Photobucket so you can cancel the subscription without losing anything.

Photobucket made bulk export deliberately painful starting around 2017. They paywalled it, hid the option, and at one point were charging a $50 fee just to package up your own files into a zip. This is the workaround. It uses the same private GraphQL API the official web app talks to, then grabs each file from the underlying S3 bucket. What you end up with is byte-exact to what you uploaded, not the slightly recompressed copy the website serves.

## Does it still work?

Last verified late 2026 against `app.photobucket.com`. The API isn't public so a single field rename on their end could break it. Open an issue if it does.

## Install

```bash
pip install photobucket-dl

# no install:
uvx photobucket-dl --help

# optional, lets the tool grab the auth cookie itself:
pip install 'photobucket-dl[browser]'
```

Python 3.10+.

## Auth cookie

Every request needs the value of your `app_auth` cookie, which is a short-lived Firebase JWT. To grab it:

1. Sign in to <https://app.photobucket.com>.
2. Hit F12 for DevTools.
3. Application or Storage tab (depending on browser), then Cookies, then `https://app.photobucket.com`.
4. Find `app_auth` and copy its value. Long string starting with `eyJ`.

The cookie expires after about an hour. If a download dies halfway with an auth error, refresh the page in your browser, copy the new value, and rerun. Anything already on disk gets skipped.

If you installed the `browser` extra it'll try to read the cookie from Firefox, Chrome, Chromium, Edge or Brave directly and you can skip the DevTools dance.

## Use it

```bash
photobucket-dl --cookie 'eyJ...' -o ./my-photos

# or from a file
photobucket-dl --cookie-file ./auth.txt -o ./my-photos

# or env var
PHOTOBUCKET_AUTH='eyJ...' photobucket-dl -o ./my-photos

# or with [browser] installed, no cookie argument
photobucket-dl -o ./my-photos
```

Output lands in `./my-photos/<bucket title>/<album path>/<filename>`. A `manifest.json` also gets written into the output dir with a record of every item the tool saw, which is worth keeping once the Photobucket account is gone.

## A note on resolution

The obvious URL field on each photo (`imageUrl`) is a CDN copy. Same pixel dimensions, but JPEGs come back silently recompressed, usually 10-25% smaller than the file you originally uploaded. This tool ignores it. Instead it asks Photobucket for a presigned S3 URL pointing at the real original (`signedUrl` on `BucketMediaByIds`) and saves that. What lands on disk matches what you uploaded byte for byte.

## Caveats

Only works on accounts you can sign in as. No path to anyone else's data, and that's intentional.

If your library is huge, the JWT may expire mid-run. You'll get a clear error message, grab a fresh cookie, rerun, resume is built in.

Videos download the same way as photos. If you have a lot of them, the bandwidth bill is yours, not Photobucket's.

The GraphQL operations were lifted out of the compiled JS bundle on app.photobucket.com. If they rename a field, things break. PRs welcome.

## Legal-ish

Unofficial. Use it on accounts you own. Doing this almost certainly violates Photobucket's terms of service. I don't work for them and I'm not responsible for what you do with this.

## License

MIT, see [LICENSE](LICENSE).
