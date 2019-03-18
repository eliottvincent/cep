#!/usr/bin/env python3
# -*- encoding: utf-8 -*-
import sys
import os
import re
import csv
from decimal import Decimal as D
from pprint import pprint
from datetime import datetime
from pathlib import Path


# - will match owner
owner_regex = r'Identifiant client\s+(?P<owner>\D*)'
owner_regex = r'^(?P<title>MR|MME|MLLE)\s+(?P<owner>\D*?)$'

# - will match dates
emission_date_regex = r'\b(?P<date>[\d/]{10})\b'

# - will match debits
# 18/10 CB CENTRE LECLERC  FACT 161014      13,40
debit_regex = r'^(?P<op_dte>\d\d\/\d\d)(?P<op_dsc>.*?)\s+(?P<op_amt>\d{1,3}\s{1}\d{1,3}\,\d{2}|\d{1,3}\,\d{2})$'

# - will match credits
# 150,0008/11 VIREMENT PAR INTERNET
credit_regex = r'^(?P<op_amt>\d{1,3}\s{1}\d{1,3}\,\d{2}|\d{1,3}\,\d{2})(?P<op_dte>\d\d\/\d\d)(?P<op_dsc>.*)$'

# - will match previous account balances (including date and balance)
#   SOLDE PRECEDENT AU 15/10/14 56,05
#   SOLDE PRECEDENT AU 15/10/14 1 575,00
#   SOLDE PRECEDENT   0,00
previous_balance_regex = r'SOLDE PRECEDENT AU (?P<bal_dte>\d\d\/\d\d\/\d\d)\s+(?P<bal_amt>[\d, ]+?)$'

# - will match new account balances
#   NOUVEAU SOLDE CREDITEUR AU 15/11/14 (en francs : 1 026,44) 156,48
new_balance_regex = r'NOUVEAU SOLDE CREDITEUR AU (?P<bal_dte>\d\d\/\d\d\/\d\d)\s+\(en francs : (?P<bal_amt_fr>[\d, ]+)\)\s+(?P<bal_amt>[\d, ]+?)$'

# counters for stats
other_op_count = 0
bank_op_count = 0
deposit_op_count = 0
wire_transfer_op_count = 0
check_op_count = 0
card_debit_op_count = 0
withdrawal_op_count = 0
direct_debit_op_count = 0


def parse_pdf_file(filename):
    # force filename as string
    filename = str(filename)
    if filename.endswith('pdf') == False:
        return (True, None)

    print('Parsing: ' + filename)

    # escape spaces in name
    filename = re.sub(r'\s', '\\ ', filename)

    # parse pdf
    command = 'pdf2txt.py -M 200 -o tmp.txt ' + filename
    os.system(command)

    # open resulting file
    parsed_file = open('tmp.txt', 'r')

    # save reference to interact with the file outside of this function
    global current_file
    current_file = parsed_file

    # read file content and return it
    file_content = parsed_file.read()
    return (False, file_content)


def clean_statement(statement):
    # remove lines with one character or less
    re.sub(r'(\n.| +)$', '', statement, flags=re.M)
    return statement


def search_account_owner(statement):
    # search for owner to identify multiple accounts
    account_owner = re.search(owner_regex, statement, flags=re.M)
    if (not account_owner):
        raise ValueError('No account owner was found.')
    # extract and strip
    account_owner = account_owner.group('owner').strip()
    print(' * Account owner is ' + account_owner)
    return account_owner


def search_accounts(statement):
    # get owner
    owner = search_account_owner(statement)

    account_regex = r'^((?:MR|MME|MLLE) ' + owner + ' - .* - ([^(\n]*))$'
    accounts = re.findall(account_regex, statement, flags=re.M)
    print(' * There are {0} accounts:'.format(len(accounts)))

    # cleanup account number for each returned account
    # we use a syntax called 'list comprehension'
    cleaned_accounts = [(full, re.sub(r'\D', '', account_number))
                        for (full, account_number) in accounts]
    return cleaned_accounts


def search_emission_date(statement):
    emission_date = re.search(emission_date_regex, statement)
    # extract and strip
    emission_date = emission_date.group('date').strip()
    # parse date
    emission_date = datetime.strptime(
        emission_date, '%d/%m/%Y')
    print(' * Emission date is ' + emission_date.strftime('%d/%m/%Y'))
    return emission_date


def search_previous_balance(account):
    previous_balance_amount = D(0.0)
    previous_balance_date = None
    # in the case of a new account (with no history) or a first statement...
    # ...this regex won't match
    previous_balance = re.search(previous_balance_regex, account, flags=re.M)

    # if the regex matched
    if previous_balance:
        previous_balance_date = previous_balance.group('bal_dte').strip()
        previous_balance_amount = previous_balance.group('bal_amt').strip()
        previous_balance_amount = string_to_decimal(previous_balance_amount)

    if not (previous_balance_amount and previous_balance_date):
        print('⚠️  couldn\'t find a previous balance for this account')
    return (previous_balance_amount, previous_balance_date)


def search_new_balance(account):
    new_balance_amount = D(0.0)
    new_balance_date = None
    new_balance = re.search(new_balance_regex, account, flags=re.M)

    # if the regex matched
    if new_balance:
        new_balance_date = new_balance.group('bal_dte').strip()
        new_balance_amount = new_balance.group('bal_amt').strip()
        new_balance_amount = string_to_decimal(new_balance_amount)

    if not (new_balance_amount and new_balance_date):
        print('⚠️  couldn\'t find a new balance for this account')
    return (new_balance_amount, new_balance_date)


#                              _   _
#    ___  _ __   ___ _ __ __ _| |_(_) ___  _ __
#   / _ \| '_ \ / _ \ '__/ _` | __| |/ _ \| '_ \
#  | (_) | |_) |  __/ | | (_| | |_| | (_) | | | |
#   \___/| .__/ \___|_|  \__,_|\__|_|\___/|_| |_|
#        |_|
#
def set_operation_year(emission, statement_emission_date):
    # fake a leap year
    emission = datetime.strptime(emission + '00', '%d/%m%y')
    if emission.month <= statement_emission_date.month:
        emission = emission.replace(year=statement_emission_date.year)
    else:
        emission = emission.replace(year=statement_emission_date.year - 1)
    return datetime.strftime(emission, '%d/%m/%Y')


def set_operation_amount(amount, debit):
    if debit:
        return ['', decimal_to_string(amount)]
    return [decimal_to_string(amount), '']


def search_operation_type(op_description):
    op_description = op_description.upper()
    # bank fees, international fees, subscription fee to bouquet, etc.
    if ((op_description.startswith('*'))):
        type = 'BANK'
        global bank_op_count
        bank_op_count += 1
    # cash deposits on the account
    elif ((op_description.startswith('VERSEMENT'))):
        type = 'DEPOSIT'
        global deposit_op_count
        deposit_op_count += 1
    # incoming / outcoming wire transfers: salary, p2p, etc.
    elif ((op_description.startswith('VIREMENT')) or (op_description.startswith('VIR SEPA'))):
        type = 'WIRETRANSFER'
        global wire_transfer_op_count
        wire_transfer_op_count += 1
    # check deposits / payments
    elif ((op_description.startswith('CHEQUE')) or (op_description.startswith('REMISE CHEQUES')) or (op_description.startswith('REMISE CHQ'))):
        type = 'CHECK'
        global check_op_count
        check_op_count += 1
    # payments made via debit card
    elif ((op_description.startswith('CB'))):
        type = 'CARDDEBIT'
        global card_debit_op_count
        card_debit_op_count += 1
    # withdrawals
    elif ((op_description.startswith('RETRAIT DAB')) or (op_description.startswith('RET DAB'))):
        type = 'WITHDRAWAL'
        global withdrawal_op_count
        withdrawal_op_count += 1
    # direct debits
    elif ((op_description.startswith('PRLV'))):
        type = 'DIRECTDEBIT'
        global direct_debit_op_count
        direct_debit_op_count += 1
    else:
        type = 'OTHER'
        global other_op_count
        other_op_count += 1

    return type


def create_operation_entry(op_date, statement_emission_date, account_number, op_description,
                           op_amount, debit):
    # search the operation type according to its description
    op_type = search_operation_type(op_description)

    op = [
        set_operation_year(op_date, statement_emission_date),
        account_number,
        op_type,
        op_description.strip(),
        # the star '*' operator is like spread '...' in JS
        *set_operation_amount(op_amount, debit)
    ]
    return op


def string_to_decimal(str):
    # replace french separator by english one (otherwise there is a conversion syntax error)
    str = str.replace(',', '.')
    # remove useless spaces
    str = str.replace(' ', '')
    # convert to decimal
    nb = D(str)
    return nb


def decimal_to_string(dec):
    dec_as_str = str(dec)
    # replace english separator by french one
    dec_as_str = dec_as_str.replace('.', ',')
    return dec_as_str


def main():
    operations = []
    errors = 0

    # go through each file of directory
    p = Path(sys.argv[1])
    for filename in sorted(p.iterdir()):
        # 1. parse statement file
        (file_is_not_pdf, parsed_statement) = parse_pdf_file(filename)
        if file_is_not_pdf == True:
            # skip the current iteration
            continue

        # 2. clean statement content
        statement = clean_statement(parsed_statement)

        # 3. search for date of emission of the statement
        emission_date = search_emission_date(statement)

        # 4. search all accounts
        accounts = search_accounts(statement)

        # 5 loop over each account
        for (full, account_number) in reversed(accounts):
            print('   * ' + account_number)

            (statement, _, account) = statement.partition(full)

            # search for last/new balances
            (previous_balance, previous_balance_date) = search_previous_balance(account)
            (new_balance, new_balance_date) = search_new_balance(account)
            # create total for inconsistency check
            total = D(0.0)

            # search all debit operations
            debit_ops = re.finditer(debit_regex, account, flags=re.M)
            for debit_op in debit_ops:
                # extract regex groups
                op_date = debit_op.group('op_dte').strip()
                op_description = debit_op.group('op_dsc').strip()
                op_amount = debit_op.group('op_amt').strip()
                # convert amount to regular Decimal
                op_amount = string_to_decimal(op_amount)
                # update total
                total -= op_amount
                # print('debit {0}'.format(op_amount))
                operations.append(create_operation_entry(op_date, emission_date,
                                                         account_number, op_description, op_amount, True))

            # search all credit operations
            credit_ops = re.finditer(credit_regex, account, flags=re.M)
            for credit_op in credit_ops:
                # extract regex groups
                op_date = credit_op.group('op_dte').strip()
                op_description = credit_op.group('op_dsc').strip()
                op_amount = credit_op.group('op_amt').strip()
                # convert amount to regular Decimal
                op_amount = string_to_decimal(op_amount)
                # update total
                total += op_amount
                # print('credit {0}'.format(op_amount))
                operations.append(create_operation_entry(op_date, emission_date,
                                                         account_number, op_description, op_amount, False))

            # check inconsistencies
            if not ((previous_balance + total) == new_balance):
                print(
                    '⚠️  inconsistency detected between imported operations and new balance')
                errors += 1
                print('previous_balance is {0}'.format(previous_balance))
                print('predicted new_balance is {0}'.format(
                    previous_balance + total))
                print('new_balance should be {0}'.format(new_balance))
                print(account)

        current_file.close()
        print('✅ Parse ok')

    # sort everything by date
    operations.sort(key=lambda x: datetime.strptime(x[0], '%d/%m/%Y'))

    # write result in file
    with open('output.csv', 'w', newline='') as f:
        # we use ';' separator to avoid conflicts with amounts' ','
        writer = csv.writer(f, delimiter=';')
        writer.writerows(
            [['date', 'account', 'type', 'description', 'credit', 'debit'], *operations]
        )
    print('OPERATIONS({0})'.format(len(operations)))
    print(
        'OTHER({0})/BANK({1})/DEPOSIT({2})/WIRETRANSFER({3})/CHECK({4})/CARDDEBIT({5})/WITHDRAWAL({6})/DIRECTDEBIT({7})'
        .format(
            other_op_count,
            bank_op_count,
            deposit_op_count,
            wire_transfer_op_count,
            check_op_count,
            card_debit_op_count,
            withdrawal_op_count,
            direct_debit_op_count
        )
    )
    print('ERRORS({0})'.format(errors))

    # rm tmp file
    os.remove('tmp.txt')


if __name__ == "__main__":
    main()
