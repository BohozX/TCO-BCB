# TCO-BCB

Base histórica, diaria del **Tipo de Cambio Oficial (TCO)** del dólar
estadounidense y de las **operaciones cambiarias del sistema bancario boliviano**,
construida automáticamente a partir de las publicaciones del Banco Central de Bolivia.

`datos/detalle.csv` se arma con los cortes guardados en `datos/raw.zip`
(`operaciones/AAAA-MM-DD.csv`, endpoint público del BCB): precio (Bs/USD) y monto
(USD) por banco y fecha de corte, tal como los publica el BCB (solo se pasa la coma
decimal a punto y se quita el separador de miles).
