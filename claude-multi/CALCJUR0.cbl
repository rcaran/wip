      ******************************************************************
      * CALCJUR0 - Calculo de Juros e Multas por Atraso
      * AUTHOR: JOSE SILVA
      * DATE-WRITTEN: 15/03/1998
      * REMARKS: Calcula juros compostos e multa para titulos vencidos
      ******************************************************************
       IDENTIFICATION DIVISION.
       PROGRAM-ID. CALCJUR0.
       AUTHOR. JOSE SILVA.
       DATE-WRITTEN. 15/03/1998.

       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT ARQ-TITULOS ASSIGN TO 'TITULOS'.
           SELECT ARQ-SAIDA   ASSIGN TO 'SAIDA'.

       DATA DIVISION.
       FILE SECTION.
       FD ARQ-TITULOS.
       01 REG-TITULO.
          05 TIT-NUMERO       PIC 9(10).
          05 TIT-VALOR        PIC 9(13)V99.
          05 TIT-VENCIMENTO   PIC 9(8).
          05 TIT-CLIENTE      PIC X(20).
          05 TIT-TIPO         PIC X(01).
             88 TITULO-NORMAL    VALUE 'N'.
             88 TITULO-ESPECIAL  VALUE 'E'.
             88 TITULO-ISENTO    VALUE 'I'.

       FD ARQ-SAIDA.
       01 REG-SAIDA.
          05 OUT-NUMERO       PIC 9(10).
          05 OUT-VALOR-ORIG   PIC 9(13)V99.
          05 OUT-JUROS        PIC 9(13)V99.
          05 OUT-MULTA        PIC 9(13)V99.
          05 OUT-TOTAL        PIC 9(13)V99.

       WORKING-STORAGE SECTION.
       01 WS-FLAGS.
          05 WS-FIM-ARQUIVO   PIC X(01) VALUE 'N'.
             88 FIM-ARQUIVO      VALUE 'S'.
             88 CONTINUA-LEITURA VALUE 'N'.

       01 WS-DATAS.
          05 WS-DATA-HOJE     PIC 9(8).
          05 WS-DIAS-ATRASO   PIC 9(4) VALUE ZERO.

       01 WS-CALCULO.
          05 WS-TAXA-JUROS    PIC 9(3)V9(6) VALUE 0.000082.
          05 WS-TAXA-MULTA    PIC 9(3)V99   VALUE ZERO.
          05 WS-JUROS-CALC    PIC 9(13)V99  VALUE ZERO.
          05 WS-MULTA-CALC    PIC 9(13)V99  VALUE ZERO.
          05 WS-TOTAL-PAGAR   PIC 9(13)V99  VALUE ZERO.
          05 WS-FATOR-JUROS   PIC 9(1)V9(8) VALUE ZERO.
          05 WS-CONTADORES.
             10 WS-QTD-LIDOS      PIC 9(7) VALUE ZERO.
             10 WS-QTD-CALCULADOS PIC 9(7) VALUE ZERO.
             10 WS-QTD-ISENTOS    PIC 9(7) VALUE ZERO.

       01 WS-LIMITE-JUROS     PIC 9(3)V99 VALUE 100.00.
       01 WS-LIMITE-MULTA     PIC 9(3)V99 VALUE 10.00.

       LINKAGE SECTION.
       01 LK-PARAMETROS.
          05 LK-DATA-PROCESSO PIC 9(8).
          05 LK-RETORNO       PIC 9(2).
             88 RETORNO-OK       VALUE 00.
             88 RETORNO-ERRO     VALUE 01 THRU 99.

       PROCEDURE DIVISION USING LK-PARAMETROS.

       0000-PRINCIPAL.
           PERFORM 1000-INICIALIZAR
           PERFORM 2000-PROCESSAR UNTIL FIM-ARQUIVO
           PERFORM 9000-FINALIZAR
           GOBACK.

       1000-INICIALIZAR.
           MOVE LK-DATA-PROCESSO TO WS-DATA-HOJE
           OPEN INPUT  ARQ-TITULOS
           OPEN OUTPUT ARQ-SAIDA
           PERFORM 1100-LER-PROXIMO.

       1100-LER-PROXIMO.
           READ ARQ-TITULOS
               AT END MOVE 'S' TO WS-FIM-ARQUIVO
           END-READ.

       2000-PROCESSAR.
           ADD 1 TO WS-QTD-LIDOS
           IF TITULO-ISENTO
               ADD 1 TO WS-QTD-ISENTOS
               PERFORM 1100-LER-PROXIMO
           ELSE
               PERFORM 2100-CALCULAR-ATRASO
               PERFORM 2200-CALCULAR-ENCARGOS
               PERFORM 2300-GRAVAR-SAIDA
               PERFORM 1100-LER-PROXIMO
           END-IF.

       2100-CALCULAR-ATRASO.
           COMPUTE WS-DIAS-ATRASO =
               FUNCTION INTEGER-OF-DATE(WS-DATA-HOJE) -
               FUNCTION INTEGER-OF-DATE(TIT-VENCIMENTO)
           IF WS-DIAS-ATRASO < 0
               MOVE ZERO TO WS-DIAS-ATRASO
           END-IF.

       2200-CALCULAR-ENCARGOS.
           IF WS-DIAS-ATRASO = 0
               MOVE ZERO TO WS-JUROS-CALC
                            WS-MULTA-CALC
                            WS-TOTAL-PAGAR
           ELSE
               PERFORM 2210-CALCULAR-JUROS
               PERFORM 2220-CALCULAR-MULTA
               COMPUTE WS-TOTAL-PAGAR =
                   TIT-VALOR + WS-JUROS-CALC + WS-MULTA-CALC
           END-IF.

       2210-CALCULAR-JUROS.
           EVALUATE TRUE
               WHEN TITULO-ESPECIAL
                   COMPUTE WS-TAXA-JUROS = WS-TAXA-JUROS / 2
               WHEN OTHER
                   CONTINUE
           END-EVALUATE
           COMPUTE WS-FATOR-JUROS =
               (1 + WS-TAXA-JUROS) ** WS-DIAS-ATRASO
           COMPUTE WS-JUROS-CALC =
               TIT-VALOR * WS-FATOR-JUROS - TIT-VALOR
           IF WS-JUROS-CALC > WS-LIMITE-JUROS
               MOVE WS-LIMITE-JUROS TO WS-JUROS-CALC
           END-IF.

       2220-CALCULAR-MULTA.
           IF WS-DIAS-ATRASO > 30
               MOVE 10.00 TO WS-TAXA-MULTA
           ELSE
               IF WS-DIAS-ATRASO > 5
                   MOVE 2.00 TO WS-TAXA-MULTA
               ELSE
                   MOVE 0.00 TO WS-TAXA-MULTA
               END-IF
           END-IF
           COMPUTE WS-MULTA-CALC =
               TIT-VALOR * WS-TAXA-MULTA / 100
           IF WS-MULTA-CALC > WS-LIMITE-MULTA
               MOVE WS-LIMITE-MULTA TO WS-MULTA-CALC
           END-IF.

       2300-GRAVAR-SAIDA.
           MOVE TIT-NUMERO   TO OUT-NUMERO
           MOVE TIT-VALOR    TO OUT-VALOR-ORIG
           MOVE WS-JUROS-CALC TO OUT-JUROS
           MOVE WS-MULTA-CALC TO OUT-MULTA
           MOVE WS-TOTAL-PAGAR TO OUT-TOTAL
           WRITE REG-SAIDA
           ADD 1 TO WS-QTD-CALCULADOS.

       9000-FINALIZAR.
           DISPLAY 'TITULOS LIDOS:      ' WS-QTD-LIDOS
           DISPLAY 'TITULOS CALCULADOS: ' WS-QTD-CALCULADOS
           DISPLAY 'TITULOS ISENTOS:    ' WS-QTD-ISENTOS
           CLOSE ARQ-TITULOS
                 ARQ-SAIDA
           MOVE 00 TO LK-RETORNO.

       9999-PARAGRAFO-MORTO.
           DISPLAY 'ESTE PARAGRAFO NUNCA E EXECUTADO'
           MOVE 99 TO LK-RETORNO.
