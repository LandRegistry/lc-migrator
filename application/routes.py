from application.data import migrate_record, connect_to_psql, disconnect_from_psql, create_cursor, close_cursor, commit, rollback
#import json
import logging
import traceback
#import threading
import requests
import operator
import re
#import time
from datetime import datetime, timedelta
import time
from application.utility import convert_class, class_without_brackets, parse_amend_info, save_to_file, reformat_county, \
    extract_authority_name


app_config = None
final_log = []
error_queue = None
wait_time_legacydb = 0
wait_time_sqlinsert = 0
sqlinsert_count = 0
wait_time_manipulation = 0
call_count_legacy_db = 0
legacy_db_ttfb = 0


class MigrationException(RuntimeError):
    def __init__(self, message, text=None):
        super(RuntimeError, self).__init__(message)
        self.text = text


def get_from_legacy_adapter(url, headers={}, params={}):
    start = time.perf_counter()
    response = requests.get(url, headers=headers, params=params)
    global wait_time_legacydb
    global call_count_legacy_db
    global legacy_db_ttfb
    wait_time_legacydb += time.perf_counter() - start
    legacy_db_ttfb += response.elapsed.total_seconds()
    
    call_count_legacy_db += 1
    return response


def get_registrations_to_migrate(start_date, end_date):
    url = app_config['LEGACY_ADAPTER_URI'] + '/land_charges/' + start_date + '/' + end_date
    headers = {'Content-Type': 'application/json'}
    logging.info("GET %s", url)

    response = get_from_legacy_adapter(url, headers=headers, params={'type': 'NR'})
    logging.info("Responses: %d", response.status_code)
    
    if response.status_code == 200:
        list = response.json()
        logging.info("Found %d items", len(list))
        return list
    else:
        raise MigrationException("Unexpected response {} from {}".format(response.status_code, url))
    # return [{
        # "reg_no": "1416",
        # "date": "2002-04-16",
        # "class": "D2"
    # }]
    # return [{ 
        # "reg_no": "100",
        # "date": "2011-10-10",
        # "class": "PAB"
    # }]

    

# TODO: Important! Can we have duplicate rows on T_LC_DOC_INFO with matching reg number and date???

def get_doc_history(reg_no, class_of_charge, date):
    url = app_config['LEGACY_ADAPTER_URI'] + '/doc_history/' + reg_no
    headers = {'Content-Type': 'application/json'}
    logging.info("  GET %s?class=%s&date=%s", url, class_without_brackets(class_of_charge), date)
    response = get_from_legacy_adapter(url, headers=headers, params={'class': class_without_brackets(class_of_charge), 'date': date})
    logging.info('  Response: %d', response.status_code)
    
    if response.status_code != 200:
        logging.warning("Non-200 return code {} for {}".format(response.status_code, url))

    if response.status_code == 404:
        return None

    return response.json()


def get_land_charge(reg_no, class_of_charge, date):
    url = app_config['LEGACY_ADAPTER_URI'] + '/land_charges/' + str(reg_no)
    headers = {'Content-Type': 'application/json'}
    logging.info('    GET %s?class=%s&date=%s', url, class_of_charge, date)
    response = get_from_legacy_adapter(url, headers=headers, params={'class': class_of_charge, 'date': date})

    logging.info('    Response: %d', response.status_code)
    if response.status_code == 200:
        return response.json()
    elif response.status_code == 404:
        return None
    else:
        raise MigrationException("Unexpected response {} from {}".format(response.status_code, url),
                                 response.text)


def add_flag(data, flag):
    for item in data:
        item['migration_data']['flags'].append(flag)


def flag_oddities(data):
    # There aren't many circumstances when we can't migrate something - often source data consists only
    # of registration number, date and class of charge (i.e. rest of the data is in the form image)
    # Flag the oddities up anyway so we can check out any data quality issues
    # logging.debug("Data:")
    # logging.debug(data)
    if data[0]['type'] != 'NR':
        add_flag(data, "Does not start with NR")

    for item in data:
        if item['type'] == 'NR':
            if item != data[0]:
                add_flag(data, "NR is not the first item")
            # if item['migration_data']['original']['registration_no'] != item['registration']['registration_no'] or \
               # item['migration_data']['original']['date'] != item['registration']['date']:
                # add_flag(data, "NR has inconsitent original details")
    
    if len(data[-1]['parties']) == 0:
        add_flag(data, "Last item lacks name information")
                

def log_item_summary(data):
    global final_log
    for item in data:
        final_log.append("Processed " + item['registration']["date"] + "/" + str(item['registration']['registration_no']))
        for flag in item['migration_data']['flags']:
            final_log.append("  " + flag)
               

def check(config, start, end):
    global app_config
    app_config = config
    #print(app_config)

    url = "{}/land_charges_index/{}/{}".format(config['LEGACY_ADAPTER_URI'], start, end)
    headers = {'Content-Type': 'application/json'}
    registrations = get_from_legacy_adapter(url, headers=headers).json()

    try:
        conn = connect_to_psql(config['PSQL_CONNECTION'])
        cursor = create_cursor(conn)

        for reg in registrations:
            #output = ''
            output = "{}\t{}\t{}\t".format(reg['registration_no'], reg['registration_date'], reg['class_type'])

            reg_no = re.sub("[^0-9]", "", str(reg['registration_no']))
            if str(reg['registration_no']) != reg_no:
                cursor.execute('SELECT r.id, r.registration_no, r.date, r.expired_on, rd.class_of_charge '
                               'FROM register r, register_details rd, migration_status ms '
                               'WHERE r.details_id=rd.id and r.id = ms.register_id and r.registration_no=%(nno)s and '
                               'ms.original_regn_no=%(no)s and r.date=%(date)s and rd.class_of_charge=%(cls)s ', {
                                   'no': reg['registration_no'].strip(), 'date': reg['registration_date'], 'cls': class_without_brackets(reg['class_type']),
                                   'nno': reg_no.strip()
                               })
            else:
                cursor.execute('SELECT r.id, r.registration_no, r.date, r.expired_on, rd.class_of_charge '
                               'FROM register r, register_details rd '
                               'WHERE r.details_id=rd.id and r.registration_no=%(no)s and r.date=%(date)s and '
                               'rd.class_of_charge=%(cls)s', {
                                   'no': reg['registration_no'].strip(), 'date': reg['registration_date'], 'cls': class_without_brackets(reg['class_type'])
                               })

            rows = cursor.fetchall()
            if len(rows) == 0:
                # output += "  No rows found\t"
                print(output + " no rows")
            # else:
            #     output += "  {} rows found\t".format(len(rows))
            #     for row in rows:
            #         output += "  {}\t{}".format(row['id'], row['expired_on'])

            # output += "END {} {} {}\n".format(reg['registration_no'], reg['registration_date'], reg['class_type'])
            # output += "\n"
            # print(output)

    finally:
        if cursor is not None:
            commit(cursor)
            close_cursor(cursor)

        if conn is not None:
            disconnect_from_psql(conn)



def migrate(config, start, end):
    global app_config
    global error_queue
    global sqlinsert_count
    app_config = config

    # hostname = app_config['AMQP_URI']
    # connection = kombu.Connection(hostname=hostname)
    # error_queue = connection.SimpleQueue('errors')

    logging.info('Migration started')
    total_start = time.perf_counter()
    
    
    
    error_count = 0
    # Get all the registration numbers that need to be migrated

    # start_time = time.perf_counter()
    # try:
        # reg_data = get_registrations_to_migrate(start, end)
        # if not isinstance(reg_data, list):
            # msg = "Registration data is not a list:"
            # logging.error(msg)
            # logging.error(reg_data)
            # report_error("E", msg, json.dumps(reg_data))
            # return

        # total_read = len(reg_data)
        # logging.info("Retrieved %d items from /land_charges", total_read)

    # except Exception as e:
        # logging.error('Unhandled exception: %s', str(e))
        # report_exception(e)
        # raise
    # wait_time_get_regs = time.perf_counter() - start_time
    wait_time_get_regs = 0
    total_inc_history = 0
    total_read = 0
    registrations = []
    
    cdate = datetime.fromtimestamp(time.mktime(time.strptime(start, '%Y-%m-%d')))
    edate = datetime.fromtimestamp(time.mktime(time.strptime(end, '%Y-%m-%d')))
    while cdate <= edate:
        logging.info("Process %s", cdate.strftime('%Y-%m-%d'))
        
        get_start = time.perf_counter()
        url = app_config['LEGACY_ADAPTER_URI'] + '/land_charges_data/' + cdate.strftime('%Y-%m-%d')
        headers = {'Content-Type': 'application/json'}
        day_regs = get_from_legacy_adapter(url, headers=headers).json()
        total_read += len(day_regs)
        wait_time_get_regs += get_start - time.perf_counter()
        
        cdate += timedelta(days=1)
        for history in day_regs:
            # Reg is equivalend to history...
            logging.debug(history)
            try:
    

                # for rows in reg_data:
                # try:
                # #For each registration number returned, get history of that application.
                # logging.info("------------------------------------------------------------------")
                # rows['class'] = rows['class'].strip()
                # rows['reg_no'] = rows['reg_no'].strip()
                # rows['date'] = rows['date'].strip()
                # logging.info("Process %s %s/%s", rows['class'], rows['date'], rows['reg_no'])

                # history = get_doc_history(rows['reg_no'], rows['class'], rows['date'])
                
                start = time.perf_counter()
                global wait_time_manipulation
                
                if history is None or len(history) == 0:
                    logging.error("  No document history information found")
                    continue

                total_inc_history += len(history)
                for i in history:
                    i['sorted_date'] = datetime.strptime(i['date'], '%Y-%m-%d').date()
                
                logging.info("  Chain of length %d found", len(history))
                history.sort(key=operator.itemgetter('sorted_date', 'reg_no'))

                this_register = []
                for x, registers in enumerate(history):
                    registers['class'] = convert_class(registers['class'])

                    logging.info("    Historical record %s %s %s", registers['class'], registers['reg_no'],
                                 registers['date'])


                    #numeric_reg_no = int(re.sub("/", "", registers['reg_no'])) # TODO: is this safe?
                    land_charges = registers['land_charge']
                        #get_land_charge(numeric_reg_no, registers['class'], registers['date'])
                   
                    if land_charges is not None and len(land_charges) > 0:
                        records = extract_data(land_charges, registers['type'])

                        #this_register.append(record)
                        this_register += records

                        
                    else:
                        record = build_dummy_row(registers)
                        #registrations.append(record)
                        this_register.append(record)

                flag_oddities(this_register)
                #save_to_file(this_register)
                wait_time_manipulation += time.perf_counter() - start
                registrations.append(this_register)

                if len(registrations) > 20:
                    registration_failures = insert_record_to_db(config, registrations)
                    registrations = []

                    if len(registration_failures) > 0:
                        logging.error('Failed migrations:')
                        for fail in registration_failures:
                            logging.error("Registration {} of {}".format(fail['number'], fail['date']))
                            logging.error(fail['message'])
                            error_count += 1
                            final_log.append('Failed to migrate ' + fail["date"] + "/" + str(fail['number']))
                    else:
                        log_item_summary(registrations)

            except Exception as e:
                logging.error('Unhandled exception: %s', str(e))
                logging.error('Failed to migrate  %s %s %s', history[0]['class'], history[0]['reg_no'], history[0]['date'])
                report_exception(e)
                error_count += 1

    # End of main loop
    # TODO: repeated code
    if len(registrations) > 0:
        registration_failures = insert_record_to_db(config, registrations)
        registrations = []

        if len(registration_failures) > 0:
            logging.error('Failed migrations:')
            for fail in registration_failures:
                logging.error("Registration {} of {}".format(fail['number'], fail['date']))
                logging.error(fail['message'])
                error_count += 1
                final_log.append('Failed to migrate ' + fail["date"] + "/" + str(fail['number']))
        else:
            log_item_summary(registrations)


    
    global wait_time_legacydb
    global legacy_db_ttfb
    total_time = time.perf_counter() - total_start

    logging.info('Migration complete')
    logging.info("Total registrations read: %d", total_read)
    logging.info("Total records processed: %d", total_inc_history)
    logging.info("Total errors: %d", error_count)
    logging.info("Legacy Adapter wait time: %f (%d calls)", wait_time_legacydb, call_count_legacy_db)
    logging.info("Legacy Adapter cumulative TTFB: %f", legacy_db_ttfb)
    #logging.info("Startup wait time: %f", wait_time_get_regs)
    logging.info("SQL Insert wait time: %f", wait_time_sqlinsert)
    logging.info("Data Mangling wait time: %f", wait_time_manipulation)
    logging.info("Total run time: %f", total_time)
    
    for line in final_log:
        logging.info(line)

    
def insert_record_to_db(config, data):
    start = time.perf_counter()
    failures = migrate_record(config, data)
    global wait_time_sqlinsert
    global sqlinsert_count
    sqlinsert_count += 1
    wait_time_sqlinsert += time.perf_counter() - start
    return failures

        
def report_exception(exception):
    global error_queue
    call_stack = traceback.format_exc()
    logging.error(call_stack)
    
    error = {
        "type": "E",
        "message": str(exception),
        "stack": call_stack,
        "subsystem": app_config["APPLICATION_NAME"]
    }
    # TODO: also report exception.text
    # error_queue.put(error)


def report_error(error_type, message, stack):
    global error_queue
    logging.info('ERROR: ' + message)

    error = {
        "type": error_type,
        "message": message,
        "subsystem": app_config["APPLICATION_NAME"],
        "stack": stack
    }
    error_queue.put(error)


def extract_data(rows, app_type):
    #print(rows)
    data = rows[0]

    if data['reverse_name_hex'][-2:] == '01':
        # County council
        logging.info('      EO Name is County Council')
        registration = build_registration(data, 'County Council', extract_authority_name(data['name']))
    elif data['reverse_name_hex'][-2:] == '02':
        # Rural council
        logging.info('      EO Name is Rural Council')
        registration = build_registration(data, 'Rural Council', extract_authority_name(data['name']))
    elif data['reverse_name_hex'][-2:] == '04':
        # Parish council
        logging.info('      EO Name is Parish Council')
        registration = build_registration(data, 'Parish Council', extract_authority_name(data['name']))
    elif data['reverse_name_hex'][-2:] == '08':
        # Other council
        logging.info('      EO Name is Other Council')
        registration = build_registration(data, 'Other Council', extract_authority_name(data['name']))
    elif data['reverse_name_hex'][-2:] == '10':
        # Dev corp
        logging.info('      EO Name is Development Corporation')
        registration = build_registration(data, 'Development Corporation', {'other': data['name']})
    elif data['reverse_name_hex'][-2:] == 'F1':
        # Ltd Company
        logging.info('      EO Name is Limited Company')
        registration = build_registration(data, 'Limited Company', {'company': data['name']})
    elif data['reverse_name_hex'][-2:] == 'F2':
        # Other
        logging.info('      EO Name is Other')
        registration = build_registration(data, 'Other', {'other': data['name']})
    elif data['reverse_name_hex'][-2:] == 'F3' and data['reverse_name_hex'][0:2] == 'F9':
        logging.info('      EO Name is Complex Name')
        registration = build_registration(data, 'Complex Name', {'complex': {'name': data['name'], 'number': int(data['reverse_name_hex'][2:8], 16)}})
    else:    
        # Mundane name
        logging.info('      EO Name is Simple')
        registration = extract_simple(data)
    
    registration['type'] = app_type

    addl_rows = []
    if len(rows) > 1:
        addl_rows = handle_additional_rows(registration, rows, app_type)

    return [registration] + addl_rows


def whats_different(row1, row2):
    changes = []
    for key in row1:
        if row1[key] != row2[key]:
            changes.append(key)

    return changes


def handle_additional_rows(registration, rows, app_type):
    # It's possible for the data to turn up some interesting variants where we have multiple index entries for
    # a registration. This is uncommon (about 1.3% of the entries), but 1.3% of several million is still a good
    # number of rows.
    # Identified cases:
    #   Additional county           add extra county to existing row
    #   Additional name             add extra names
    #   Additional class of charge  add new registration
    add_regs = []

    #"migration_data": {
    #registration['migration_data']['additional_data'] = []
    additional_data = {}

    for row in rows[1:]:
        changes = whats_different(row, rows[0])
        if "class_type" in changes:
            add_regs.append(extract_data([row], app_type))

        else:
            # Lovely unrolled loop... well, it's a one-off
            if "amendment_info" in changes:
                if 'amendment_info' not in additional_data:
                    additional_data['amendment_info'] = []
                additional_data['amendment_info'].append(row['amendment_info'])

            if "priority_notice" in changes:
                if 'priority_notice' not in additional_data:
                    additional_data['priority_notice'] = []
                additional_data['priority_notice'].append(row['priority_notice'])

            if "parish_district" in changes:
                if 'parish_district' not in additional_data:
                    additional_data['parish_district'] = []
                additional_data['parish_district'].append(row['parish_district'])

            if "address" in changes:
                if 'address' not in additional_data:
                    additional_data['address'] = []
                additional_data['address'].append(row['address'])

            if "property" in changes:
                if 'property' not in additional_data:
                    additional_data['property'] = []
                additional_data['property'].append(row['property'])

            if "name" in changes:
                if 'name' not in additional_data:
                    additional_data['name'] = []
                additional_data['name'].append(row['name'])

            if "occupation" in changes:
                if 'occupation' not in additional_data:
                    additional_data['occupation'] = []
                additional_data['occupation'].append(row['occupation'])

            if "priority_notice_ref" in changes:
                if 'priority_notice_ref' not in additional_data:
                    additional_data['priority_notice_ref'] = []
                additional_data['priority_notice_ref'].append(row['priority_notice_ref'])

            if "counties" in changes:
                if 'counties' not in additional_data:
                    additional_data['counties'] = []
                additional_data['counties'].append(row['counties'])

            if "reverse_name" in changes or "remainder_name" in changes or "punctuation_code" in changes:
                alt_regn = extract_data([row], app_type)[0]
                if len(alt_regn['parties']) > 0:
                    # logging.debug('Copying names...')
                    for name in alt_regn['parties'][0]['names']:
                        registration['parties'][0]['names'].append(name)

            if "property_county" in changes and 'particulars' in registration:
                if row['property_county'] not in ['BANKS', 'NO COUNTY', 'NO COUNTIES'] and row['property_county'] not in registration['particulars']['counties']:
                    registration['particulars']['counties'].append(reformat_county(row['property_county']))

    registration['migration_data']['additional_rows'] = additional_data

    return add_regs


def extract_simple(rows):
    hex_codes = []
    length = len(rows['punctuation_code'])
    count = 0
    while count < length:
        hex_codes.append(rows['punctuation_code'][count:(count + 2)])
        count += 2

    orig_name = rows["remainder_name"] + rows["reverse_name"][::-1]
    name_list = []
    for items in hex_codes:
        punc, pos = hex_translator(items)
        name_list.append(orig_name[:pos])
        name_list.append(punc)
        orig_name = orig_name[pos:]

    name_list.append(orig_name)
    full_name = ''.join(name_list)
    try:
        surname_pos = full_name.index('*')
        forenames = full_name[:surname_pos]
        surname = full_name[surname_pos + 1:]
    except ValueError:
        surname = ""
        forenames = full_name

    forenames = forenames.split()

    registration = build_registration(rows, 'Private Individual', {'private': {'forenames': forenames, 'surname': surname}})
    return registration

    
def build_dummy_row(entry):
    # logging.debug('Entry:')
    # logging.debug(entry)
    
    entry = {
        "registration": {
            "registration_no": re.sub("[^0-9]", "", entry['reg_no']),
            "date": entry['date']
        },
        "parties": [],
        "type": entry['type'],
        "class_of_charge": class_without_brackets(entry['class']),
        "applicant": {'name': '', 'address': '', 'key_number': '', 'reference': ''},
        "additional_information": "",
        "migration_data": {
            'unconverted_reg_no': entry['reg_no'],
            'flags': []
        }       
    }
    
    if entry['class_of_charge'] not in ['PAB', 'WOB']:
        entry['particulars'] = {
            'counties': [],
            'district': '',
            'description': ''
        }
    return entry


def build_registration(rows, name_type, name_data):
    # logging.debug('Head Entry:')
    # logging.debug(json.dumps(rows))
    
    coc = class_without_brackets(rows['class_type'])
    if coc in ['PAB', 'WOB']:
        eo_type = "Debtor"
        occupation = rows['occupation']
    else:
        eo_type = "Estate Owner"
        occupation = ''
       
    county_text = rows['property_county'].strip()
    logging.info('    County_text is "%s"', county_text)
    
    banks_county = ''
    if county_text in ['BANKS', ''] and coc in ['PA', 'WO', 'DA']: #  Special case for <1% of the data...
        banks_county = rows['counties']
        logging.info('    BANKS county of "%s"', county_text)

    if county_text in ['NO COUNTY', 'NO COUNTIES', 'BANKS']:
        county_text = ''
    
    pty_desc = rows['property']
    parish_district = rows['parish_district']
    
    registration = {
        "class_of_charge": coc,
        "registration": {
            "date": rows['registration_date'],
            "registration_no": re.sub("[^0-9]", "", str(rows['registration_no']))
        },
        "parties": [{
            "type": eo_type,
        }],
        "applicant": {
            'name': '',
            'address': '',
            'key_number': '',
            'reference': ''
        },
        "additional_information": "",
        "migration_data": {
            'unconverted_reg_no': rows['registration_no'],
            'amend_info': rows['amendment_info'],
            'flags': [],
            'bankruptcy_county': banks_county
        }
    }
    
    amend = parse_amend_info(rows['amendment_info'])
    registration['additional_information'] = amend['additional_information']
    
    if coc in ['PAB', 'WOB']:
        registration['parties'][0]['occupation'] = occupation
        registration['parties'][0]['trading_name'] = ''
        registration['parties'][0]['residence_withheld'] = False
        registration['parties'][0]['case_reference'] = amend['reference']
        registration['parties'][0]['addresses'] = []
        
        address_strings = rows['address'].split('   ')
        for address in address_strings:
            addr_obj = {
                'type': 'Residence',
                'address_string': address            
            }
            registration['parties'][0]['addresses'].append(addr_obj)
        
        if amend['court'] is not None:
            registration['parties'].append({
                'type': 'Court',
                'names': [{
                    'type': 'Other',
                    'other': amend['court']
                }]
            })       
        
    else:
        if rows['address'] is not None and rows['address'] != '':
            # Some old registers have addresses on non-PAB/WOB regns
            registration['parties'][0]['addresses'] = []
            address_strings = rows['address'].split('   ')
            for address in address_strings:
                addr_obj = {
                    'type': 'Residence',
                    'address_string': address
                }
                registration['parties'][0]['addresses'].append(addr_obj)

        registration['particulars'] = {
            'counties': [reformat_county(county_text)],
            'district': parish_district,
            'description': pty_desc
        }

    registration['parties'][0]['names'] = [name_data]
    registration['parties'][0]['names'][0]['type'] = name_type

    return registration


# def insert_data(registration):
    # json_data = registration

    # save_to_file(json_data)
    
    # url = app_config['LAND_CHARGES_URI'] + '/migrated_record'
    # headers = {'Content-Type': 'application/json'}
    # logging.info("  POST %s", url)
    # start = time.perf_counter()
    # response = requests.post(url, data=json.dumps(json_data), headers=headers)
    # global wait_time_landcharges
    # wait_time_landcharges += time.perf_counter() - start
    # logging.info("  Response: %d", response.status_code)
    
    # registration_status_code = response
    # # add code below to force errors
    # # registration_status_code = 500
    # return registration_status_code


def hex_translator(hex_code):
    mask = 0x1F
    code_int = int(hex_code, 16)
    length = code_int & mask
    punc_code = code_int >> 5
    punctuation = ['&', ' ', '-', "'", '(', ')', '*']
    return punctuation[punc_code], length

