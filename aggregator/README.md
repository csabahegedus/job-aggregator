# iOS Contract Aggregátor — pipeline (1. hét)

Natív iOS / mobil contract és freelance munkák begyűjtése egy helyre.
Terv: [../ideas/ios-contract-aggregator.md](../ideas/ios-contract-aggregator.md)

## Futtatás

```sh
python3 pipeline.py            # források lehúzása, jobs.db frissítése, digest.md generálása
python3 pipeline.py --digest   # csak a digest.md újragenerálása a DB-ből
```

Függőség: Python 3 + `requests`. Kimenet: `jobs.db` (SQLite), `digest.md` (top 50 találat).

## Források

| Forrás | Módszer | Megjegyzés |
|---|---|---|
| HN "Who is hiring?" + "Freelancer? Seeking freelancer?" | Algolia API, `author_whoishiring` szűrővel | a freelancer-szálban csak a "SEEKING FREELANCER" posztok számítanak megbízásnak |
| RemoteOK | publikus JSON API | |
| WeWorkRemotely | RSS (programming + full-stack kategória) | |
| Remotive | publikus JSON API (software-dev kategória) | a strukturált `job_type` mező bekerül a szűrendő szövegbe |
| Reddit (r/iOSProgramming, r/forhire, r/jobbit) | RSS | erős rate-limit; 429-re 3x retry backoff-fal + 10 mp szünet a subok között |

Az iOS Dev Jobs (Dave Verwer) sajnos nem forrás: e-mail/app-alapú lett, nincs publikus feedje.

## Szűrés két rétegben

1. **Heurisztika (mindig fut):** kulcsszavas mobil- (`ios|swift|swiftui|flutter|react native|...`) és contract-jelzők (`contract|freelance|hourly|...`). Szándékosan megengedő — vannak fals pozitívok.
2. **LLM-osztályozás (opcionális):** ha van `ANTHROPIC_API_KEY` és telepített `anthropic` csomag, a heurisztikán átjutó tételeket a Claude Haiku (`claude-haiku-4-5`) pontosítja strukturált kimenettel (natív iOS? contract? remote? rate?). Bekapcsolás:

```sh
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...
python3 pipeline.py
```

Csak az új (még nem látott) és mobil-gyanús tételek mennek az LLM-hez, így a költség pár cent / futás.

## Napi automatikus futtatás (GitHub Actions)

Ha a repo felkerül GitHubra, a `.github/workflows/aggregate.yml` (lásd a repo gyökerében) naponta lefuttatja és commitolja a friss `digest.md`-t. Az `ANTHROPIC_API_KEY`-t repo secretként kell megadni.

## Következő lépések (terv szerint)

- [x] további forrás (Remotive) + Reddit-retry
- [x] statikus oldal (docs/index.html → GitHub Pages, main/docs)
- [ ] 2. hét folyt.: heti e-mail digest (Buttondown/Resend)
- [ ] 3. hét: validálás (Show HN, r/iOSProgramming, iOS Dev Weekly)
