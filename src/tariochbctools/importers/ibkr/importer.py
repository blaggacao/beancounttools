import yaml
from os import path
from ibflex import client, parser, Types, enums
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal

from beancount.query import query
from beancount.parser import options
from beancount.ingest import importer
from beancount.core import data, amount
from beancount.core.number import D


class Importer(importer.ImporterProtocol):
    """An importer for Interactive Broker using the flex query service."""

    def identify(self, file):
        return 'ibkr.yaml' == path.basename(file.name)

    def file_account(self, file):
        return ''

    def extract(self, file, existing_entries):
        with open(file.name, 'r') as f:
            config = yaml.safe_load(f)
        token = config['token']
        queryId = config['queryId']

        response = client.download(token, queryId)

        root = ET.fromstring(response)
        statement = parser.parse_element(root)
        assert isinstance(statement, Types.FlexQueryResponse)

        result = []
        for divAccrual in statement.FlexStatements[0].ChangeInDividendAccruals:
            if divAccrual.code[0] != enums.Code.REVERSE and divAccrual.payDate <= date.today():
                print(divAccrual)
                print(divAccrual.exDate)
                print(divAccrual.payDate)
                print(divAccrual.quantity)
                print(divAccrual.symbol)
                print(divAccrual.currency)
                print(divAccrual.grossAmount)
                print(divAccrual.tax)
                print(divAccrual.fee)
                print(divAccrual.fxRateToBase)

                asset = divAccrual.symbol.replace('z', '')
                exDate = divAccrual.exDate
                payDate = divAccrual.payDate
                totalPayout = divAccrual.netAmount
                totalWithholding = divAccrual.tax
                currency = divAccrual.currency

                _, rows = query.run_query(
                    existing_entries,
                    options.OPTIONS_DEFAULTS,
                    'select sum(number) as quantity, account where currency="' + asset + '" and date<#"' + str(exDate) + '" group by account;')
                totalQuantity = D(0)
                for row in rows:
                    totalQuantity += row.quantity
                if totalQuantity != divAccrual.quantity:
                    raise Exception(f"Different Total Quantities Dividend: {divAccrual.quantity} vs Ours: {totalQuantity}")

                remainingPayout = totalPayout
                remainingWithholding = totalWithholding
                for row in rows[:-1]:
                    myAccount = row.account
                    myQuantity = row.quantity

                    myPayout = round(totalPayout * myQuantity / totalQuantity, 2)
                    remainingPayout -= myPayout
                    myWithholding = round(totalWithholding * myQuantity / totalQuantity, 2)
                    remainingWithholding -= myWithholding
                    result.append(self.createSingle(myPayout, myWithholding, myQuantity, myAccount, asset, currency, payDate))

                lastRow = rows[-1]
                result.append(self.createSingle(remainingPayout, remainingWithholding, lastRow.quantity, lastRow.account, asset, currency, payDate))

        return result

    def createSingle(self, payout, withholding, quantity, assetAccount, asset, currency, date):
        narration = "Dividend for " + str(quantity)
        liquidityAccount = self.getLiquidityAccount(assetAccount, asset, currency)
        incomeAccount = self.getIncomeAccount(assetAccount, asset)

        postings = [
            data.Posting(assetAccount, amount.Amount(D(0), asset), None, None, None, None),
            data.Posting(liquidityAccount, amount.Amount(payout, currency), None, None, None, None),
        ]
        if withholding > 0:
            receivableAccount = self.getReceivableAccount(assetAccount, asset)
            postings.append(
                data.Posting(receivableAccount, amount.Amount(withholding, currency), None, None, None, None)
            )
        if currency != 'CHF':
            price = amount.Amount(Decimal("0.99"), "CHF")
        else:
            price = None
        postings.append(
            data.Posting(incomeAccount, None, None, price, None, None)
        )

        meta = data.new_metadata('dividend', 0)
        return data.Transaction(
            meta,
            date,
            '*',
            '',
            narration,
            data.EMPTY_SET,
            data.EMPTY_SET,
            postings
        )

    def getLiquidityAccount(self, assetAccount, asset, currency):
        return assetAccount.replace(':Investment:', ':Liquidity:').replace(':' + asset, ':' + currency)

    def getReceivableAccount(self, assetAccount, asset):
        parts = assetAccount.split(':')
        return 'Assets:' + parts[1] + ':Receivable:Verrechnungssteuer'

    def getIncomeAccount(self, assetAccount, asset):
        parts = assetAccount.split(':')
        return 'Income:' + parts[1] + ':Interest'
