# Project website

The static project website is in `website/`, including the leaderboard and the already-public, sanitized researcher records. This directory is a publication export, not raw experiment output. Never put credentials or private run artifacts here.

GitHub Pages URL: https://mercury7353.github.io/self-evaluation-bench/

## One-time setup

A repository administrator must open Settings → Pages and select **GitHub Actions** as the build source. Then run **Deploy project website** from the Actions tab (or rerun its latest run). No custom domain or payment is needed for this public repository.

The workflow uses GitHub's built-in token with contents-read, pages-write, and OIDC permissions. No model APIs or provider secrets are required.

## Update and preview

Edit files under `website/` and push to `main`; the workflow publishes that directory. Internal links are relative to support repository-prefixed URLs. Leaderboard data is in `website/data.json`; published trace exports are in `website/traces/data/`.

Run `python -m http.server 8000 --directory website` and open http://localhost:8000/ to preview.

Once arXiv is public, set `paperUrl` in `website/data.json` to show the Paper link.

## Research blog

[When AI Agents Design the Test](https://mercury7353.github.io/self-evaluation-bench/blog/when-ai-agents-design-the-test.html) lives in `website/blog/when-ai-agents-design-the-test.html`. Its downloadable `.data.json` contains the aggregate chart values and source hashes, without raw responses or private paths. Keep its embedded `article-data` JSON identical to that download when updating figures. The article links the relevant public research traces.
