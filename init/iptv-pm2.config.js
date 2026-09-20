/**
 * What the EPG container runs: the server, and not the grab.
 *
 * The image's own config also starts a `grab` app on a cron and one at startup, and
 * that is the process that has never finished here — it holds every listing it has
 * fetched until it writes the file at the end, so its peak is days x channels, and
 * one day was the most this host could give it. `scripts/shard-livetv-guide.py` runs
 * the same grabber once per site instead, where each run holds one site's listings,
 * and merges the parts into the same `guide.xml` this server publishes.
 *
 * Two writers to one guide.xml is not a race worth leaving in place, so the grab
 * apps are gone rather than left to fail: this file is mounted over the image's own
 * at /epg/pm2.config.js. `npm run grab` still works inside the container, which is
 * how the shard script drives it.
 */
const apps = [
  {
    name: "serve",
    script: "npx serve -- public",
    instances: 1,
    watch: false,
    autorestart: true,
  },
];

module.exports = { apps };
