# Use the same Python and SQLite versions as the scoring environment.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS baseline

FROM baseline AS coverage-build
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tar.gz
RUN mkdir /baseline && tar -xzf /tmp/sqlglot.tar.gz -C /baseline --strip-components=1
COPY requirements.lock /build/
RUN python -m pip install --no-cache-dir -r /build/requirements.lock coverage==7.10.7
COPY build_coverage_map.py coverage_selection.py fixture_selection.py dialect_selection.py /build/
RUN python -m compileall -q /baseline/sqlglot \
 && SKIP_INTEGRATION=0 PYTHONPATH=/baseline python /build/build_coverage_map.py /baseline /build/coverage_map.json

FROM baseline
COPY --from=coverage-build /baseline/sqlglot /baseline/sqlglot
COPY --from=coverage-build /build/coverage_map.json /app/coverage_map.json
COPY selector.py coverage_selection.py fixture_selection.py semantic_selection.py dialect_selection.py fixture_map.json /app/
CMD ["python", "/app/selector.py"]
