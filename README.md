Mark Wantuck and Michael Dirksen's awesome propserity 4 algorithmic trading bot, coded fully in Python!

--Big shouts to Claude!

Also our data is here in the "data" folder... (shocker)

The "scripts" folder has all the .py and .ipynb files:

  -"round1_analysis.ipynb"is where I analyzed all the data
  
  -"round1_algo.py" is the algorithmic trading bot
  
  -"datamodel.py" is some slop from the website needed for the algo to run 
  
  -"convert_semicolon_csvs.py" is exactly what it says it does.
  

Round 1 end date: April 16th 11:59pm


Round 1 notes:\n
- The weakest part was adaptability. The code uses no state, no learning across timestamps, no reaction to fills, and no deeper use of market trades or history. In practice:\n
- the trend product was treated as “buy once and pray the drift persists”\n
- the mean-reverter was treated as “quote around a fixed local fair and hope fills come”


Improvement areas (ranked by expected impact)\n
- Widen OSM offset 1 → 3/4/5 and backtest — likely highest ROI per line of code
- Exploit lag-1 ACF via fair-value adjustment - free, novel, applies to both products
- Asymmetric skew — fix the buy/sell edge gap
- End-of-day flattening — reduces variance, helps ranking
- Multi-level passive quotes — post at fair ± 2 and fair ± 5
- Research Speed — the manual challenge's mystery pillar; check the Round 2 tutorial for latency mechanics before submitting manual


Round 2 findings thus far:

- ACF is entirely of our profit improvements for OSM so far
-   KALMAN_COEF=0.20: total=   245,836 (Δ=+2,266)   OSM=   8,025 (Δ=+2,266)
