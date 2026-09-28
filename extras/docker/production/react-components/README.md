# react-components package

`wger-project-react-components-26.8.28.tgz` is the gym frontend fork, built from
[Cub-HQ/wger-frontend@b23cd364](https://github.com/Cub-HQ/wger-frontend/tree/b23cd36458ba921fe91b448bb3aded4a2bf99189)
(itself based on upstream [wger-project/react](https://github.com/wger-project/react)).
The package keeps upstream's name and version, so `package.json` and
`package-lock.json` resolve it to this file instead of the npm registry. The
production and demo Dockerfiles refuse to build unless the tarball matches
`SHA256SUMS`; the production image records the source commit above as
`APP_UI_BUILD_COMMIT`.

Reproduce it from that commit (see fitness-coach#417):

```sh
npm ci --no-audit --no-fund
npm run build
npm pack --ignore-scripts
```

To update: rebuild from the new frontend commit, replace the tarball, regenerate
`SHA256SUMS` with `shasum -a 256`, update the `file:` path and lock integrity, and
change `UI_BUILD_COMMIT` in the production Dockerfile.
