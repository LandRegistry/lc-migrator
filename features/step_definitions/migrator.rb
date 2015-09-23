require 'net/http'
require 'json'
require 'rspec'
require 'pg'

reg_no = rand(1000..1000000).to_s # TODO: how to ensure uniqueness?
day_start = (Date.today).strftime("%Y-%m-%d")
day_end = (Date.today + 1).strftime("%Y-%m-%d")


date_range = "/begin?start_date=#{day_start}&end_date=#{day_end}"
legacy_date_range = "/land_charge?start_date=#{day_start}&end_date=#{day_end}"
#'/land_charge?start_date=2015-07-17&end_date=2015-07-18'
mytime = Time.new.strftime("%Y-%m-%d %H:%M:%S.%6N")
legacy_row = '{"time":"' + mytime + '","registration_no":"' + reg_no + '","priority_notice":"","reverse_name":"YDRUPYDDERFD","property_county":255,"registration_date":"' + day_start + '","class_type":"PA(B)","remainder_name":"STANFOR","punctuation_code":"28C6","name":"","address":"52413 LILYAN PINE EAST ILIANA FA16 0XD BERKSHIRE","occupation":"","counties":"","amendment_info":"NORTH JAMEYSHIRE COUNTY COURT 677 OF 2015","property":"","parish_district":"","priority_notice_ref":""}'




require 'date'
now = Date.today
ninety_days_ago = (now - 90)

puts reg_no
class RestAPI
	attr_reader :response, :data

    def initialize(uri)
        @uri = URI(uri)
        @http = Net::HTTP.new(@uri.host, @uri.port)
    end
    
    def post_data(url, data = nil)    
        request = Net::HTTP::Post.new(url)     
        unless data.nil?
			request.body = data
			request["Content-Type"] = "application/json"
		end
        @response = @http.request(request)
        @response.body
    end

    def get_data(url, data = nil)
        request = Net::HTTP::Get.new(url)
        unless data.nil?
			request.body = data
			request["Content-Type"] = "application/json"
		end
        @response = @http.request(request)
        @data = JSON.parse(@response.body)
    end

    def put_data(url, data)
		request = Net::HTTP::Put.new(url)
		request.body = data
		request["Content-Type"] = "application/json"
		@response = @http.request(request)
		@response.body
	end
end

class PostgreSQL
	def self.connect(database)
		@@pg = PGconn.connect( 'localhost', 5432,  '', '', database, 'vagrant', 'vagrant')
	end
	
	def self.disconnect
		@@pg.close
	end
	
	def self.query(sql)
		@@pg.exec(sql)
	end

end


migration_api = nil
legacy_api = nil
registration_api = nil

Given(/^I have inserted a row into the legacy db$/) do
	legacy_api = RestAPI.new("http://localhost:5007")
    legacy_api.put_data("/land_charge", legacy_row)
end

When(/^I submit a date range to the migrator$/) do
	migration_api = RestAPI.new("http://localhost:5009")
	migration_api.post_data(date_range)
end

When(/^I submit a date range to the legacy db$/) do
	legacy_api = RestAPI.new("http://localhost:5007")
    legacy_api.get_data(legacy_date_range)
end

When(/^it returns a 200 OK response$/) do
	expect(legacy_api.response.code).to eql "200"
end

Then(/^a new record is stored on the register database in the correct format$/) do
	PostgreSQL.connect('landcharges')
	result = PostgreSQL.query("SELECT c.party_name FROM migration_status a, register b, party_name c WHERE a.original_regn_no = #{reg_no} AND a.register_id = b.id AND b.debtor_reg_name_id = c.id FETCH FIRST 1 ROW ONLY")
	expect(result.values.length).to eq 1
	row = result.values[0]
	expect(row[0]).to eq "STANFORD FREDDY PURDY"
	PostgreSQL.disconnect
end