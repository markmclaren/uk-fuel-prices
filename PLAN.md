Rather than depending on someone else's repository staying online, you can host your own aggregator that dumps to GitHub Pages:

Register: Grab a free client ID/secret from the Fuel Finder Developer Portal.

Use an existing script: Fork a tool like [philmale/UK-Fuel-Finder](https://github.com/philmale/UK-Fuel-Finder) (uff.py).

Set up Actions:

Store your API credentials in GitHub Secrets.

Schedule an action on a cron (0 * * * * for hourly).

Let the script fetch the national stations and prices, output a normalized 
stations.geojson or prices.json, and commit it to an gh-pages branch.

Consume from your map: 

Your web map can then simply do:

fetch('https://<your-username>.github.io/<repo>/stations.geojson')

---

I want to create a Dockerfile with everything required to run uff.py in it.
I want to test it with a Docker Compose file that fetches OAuth credentials via a .env file.