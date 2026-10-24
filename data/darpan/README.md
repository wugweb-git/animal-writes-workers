Put the NGO Darpan CSV downloads here (one per state, animal-welfare category), e.g. haryana.csv, then run the workflow
"NGO Darpan import" with files = data/darpan/haryana.csv. Dry run first: it matches rows to listings of the same state (and
district when both name one) and writes a report; the real run sends proposals to Directory -> Enrichment review, source
"NGO Darpan" with the file name, Darpan ID and the date. Nothing is applied by the import.
