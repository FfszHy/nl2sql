# Third-party notices

The repository's MIT license applies to its original code. Third-party code retains its own licenses. Existing license comments in `frontend/dist/assets/*.js` are preserved.

The frontend source, package manifest, and lockfile are unavailable in this release. The following inventory was recovered from the supplied bundle; it is **not a complete software bill of materials**. Versions marked unknown have not been reconstructed from a lockfile. The stored license texts are upstream references, not a claim that the entire original dependency graph has been recovered.

| Component | Version evidence | License / local copy | Upstream source |
| --- | --- | --- | --- |
| React / React DOM | Embedded version 18.3.1 | [MIT](third_party/licenses/react-LICENSE.txt) | [React v18.3.1](https://github.com/facebook/react/blob/v18.3.1/LICENSE) |
| React JSX runtime / Scheduler | License headers; independent versions unknown | [MIT](third_party/licenses/react-LICENSE.txt) | React project |
| Apache ECharts | Embedded version 6.0.0 | [Apache-2.0 and component notices](third_party/licenses/echarts-LICENSE.txt), [NOTICE](third_party/licenses/echarts-NOTICE.txt), [d3 BSD license](third_party/licenses/echarts-LICENSE-d3.txt) | [ECharts 6.0.0](https://github.com/apache/echarts/tree/6.0.0) |
| ZRender | Embedded version 6.0.0 | [BSD-3-Clause](third_party/licenses/zrender-LICENSE.txt) | [ZRender 6.0.0](https://github.com/ecomfe/zrender/blob/6.0.0/LICENSE) |
| echarts-for-react | Wrapper identified; exact version unknown | [MIT](third_party/licenses/echarts-for-react-LICENSE.txt) | [Upstream license](https://github.com/hustcc/echarts-for-react/blob/master/LICENSE) |
| fast-deep-equal | Implementation identified; exact version unknown | [MIT reference from v3.1.3](third_party/licenses/fast-deep-equal-LICENSE.txt) | [Upstream license](https://github.com/epoberezkin/fast-deep-equal/blob/v3.1.3/LICENSE) |
| Microsoft TypeScript helpers / tslib | Microsoft license block retained; exact bundled version unknown | [License reference from tslib 2.3.0](third_party/licenses/microsoft-tslib-LICENSE.txt) | [Upstream license](https://github.com/microsoft/tslib/blob/2.3.0/LICENSE.txt) |
| size-sensor | Embedded version 1.0.3 | Upstream declares ISC; author hustcc | [Official package metadata](https://registry.npmjs.org/size-sensor/1.0.3), [upstream repository](https://github.com/hustcc/size-sensor) |

The official `size-sensor@1.0.3` package declares the ISC license but does not include a standalone LICENSE or NOTICE file. That declaration is recorded here without inventing an upstream copyright year or license file.

ECharts and ZRender 6.0.0 upstream manifests specify tslib 2.3.0. This is upstream dependency evidence, not proof of the original application's resolved dependency version.

When the original frontend sources become available, restore their manifest and lockfile, reconcile this inventory against the resolved dependencies, and regenerate the frontend with the required attribution files.
