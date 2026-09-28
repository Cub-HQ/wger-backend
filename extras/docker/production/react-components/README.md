# react-components package

`wger-project-react-components-26.8.28.tgz` is the gym frontend fork, built from
[Cub-HQ/wger-frontend@b23cd364](https://github.com/Cub-HQ/wger-frontend/tree/b23cd36458ba921fe91b448bb3aded4a2bf99189)
(itself based on upstream [wger-project/react](https://github.com/wger-project/react)).
The package keeps upstream's name and version, so it is committed here instead of
being resolved from the npm registry. The production Dockerfile refuses to build
unless the tarball matches `SHA256SUMS`, and sets `APP_UI_BUILD_COMMIT` to the
source commit above.

Reproduce it from that commit (see fitness-coach#417):

```sh
npm ci --no-audit --no-fund
npm run build
npm pack --ignore-scripts
```

To update: rebuild from the new frontend commit, replace the tarball, regenerate
`SHA256SUMS` with `shasum -a 256`, and change `UI_BUILD_COMMIT` in the Dockerfile.
