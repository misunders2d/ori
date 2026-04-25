# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Top-level package marker. The App instance is defined in app/agent.py
# but not re-exported here — importers should `from app.agent import app`
# directly. This keeps `import app.runtime.*` and `import app.util.*`
# decoupled from the full agent stack at import time (relevant during the
# clean rebuild when downstream subpackages are added incrementally).
